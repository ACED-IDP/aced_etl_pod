import json
import logging
import os
import pathlib
import shutil
import sys
import traceback
import subprocess
import orjson
import uuid

from aced_submission.meta_flat_load import DEFAULT_ELASTIC, load_flat
from aced_submission.meta_flat_load import delete as meta_flat_delete
from aced_submission.grip_load import bulk_load_raw, get_project_data, \
    delete_project as grip_delete
from opensearchpy import OpenSearchException
from gen3.auth import Gen3Auth
from gen3_tracker.meta.dataframer import LocalFHIRDatabase
from typing import List, Dict, Any, Tuple, Optional

from fhir.resources.researchstudy import ResearchStudy
from fhir.resources.documentreference import DocumentReference
from fhir.resources.documentreference import DocumentReferenceContent
from fhir.resources.attachment import Attachment
from fhir.resources.identifier import Identifier
from fhir.resources.extension import Extension

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

RESEARCH_STUDY = "ResearchStudy"
DOCUMENT_REFERENCE = "DocumentReference"

def _get_env_var(key: str, error_msg: Optional[str] = None) -> Optional[str]:
    """Get environment variable or raise error if not found."""
    value = os.environ.get(key)
    if value is None:
        raise ValueError(error_msg)
    return value

def _auth(access_token: Optional[str]) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN or default refresh token."""
    return Gen3Auth(refresh_file=f"accesstoken:///{access_token}" if access_token else None)

def _get_user(auth: Gen3Auth) -> Dict[str, Any]:
    """Get user info from arborist."""
    return auth.curl('/user/user').json()

def _parse_input_data() -> Dict[str, Any]:
    """Parse INPUT_DATA from environment."""
    return json.loads(_get_env_var('INPUT_DATA', "INPUT_DATA not found in environment"))

def _get_program_project(input_data: Dict[str, Any]) -> Tuple[str, str]:
    """Extract program and project from input_data."""
    project_id = input_data.get('projectId')
    if not project_id or '-' not in project_id:
        raise ValueError("project_id must be in the format <program>-<project>")
    return project_id.split('-')


def _can_create(output: dict,
                program: str,
                project: str,
                user: dict) -> bool:
    """Check if user can create a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        project: project Gen3 (program-)project
        user: user dict from arborist (aka profile)
    """

    can_create = True

    required_resources = [
        f"/programs/{program}",
        f"/programs/{program}/projects"
    ]
    for required_resource in required_resources:
        if required_resource not in user['resources']:
            output['logs'].append(f"{required_resource} not found in user resources")
            can_create = False
        else:
            output['logs'].append(f"HAS RESOURCE {required_resource}")

    required_services = [
        f"/programs/{program}/projects/{project}"
    ]
    for required_service in required_services:
        if required_service not in user['authz']:
            output['logs'].append(f"{required_service} not found in user authz")
            can_create = False
        else:
            if {'method': 'create', 'service': '*'} not in user['authz'][required_service]:
                output['logs'].append(f"create not found in user authz for {required_service}")
                can_create = False
            else:
                output['logs'].append(f"HAS SERVICE create on resource {required_service}")

    return can_create

def _download_and_unzip(gh_username: str,
                        gh_token: str,
                        gh_repo_url: str,
                        gh_commit_hash: str,
                        files: List[Dict],
                        bucket: str,
                        profile: str,
                        api_endpoint: str,
                        project_id: str,
                        output: Dict[str, Any],
                        dest_dir: pathlib.Path) -> bool | None:
    """
    Download and unzip META objects from Git LFS to a destination directory for loading.
    """
    try:
        repo_name = pathlib.Path(gh_repo_url).stem
        target_dir = os.path.join(os.getcwd(), repo_name)

        clone_url = f"https://{gh_username}:{gh_token}@{gh_repo_url}"
        if not _run_subprocess(["git", "clone", clone_url], os.getcwd(), output, f"ERROR CLONING {gh_repo_url}"):
            return False

        checkout_cmd = ["git", "checkout", gh_commit_hash]
        if not _run_subprocess(checkout_cmd, target_dir, output, f"ERROR CHECKING OUT for {gh_repo_url} ON HASH {gh_commit_hash}"):
            return False

        init_cmd = ["git-drs", "init", "--bucket", bucket, "--token", _get_env_var('ACCESS_TOKEN'), "--profile", profile, "--project", project_id, "--url", api_endpoint]
        if not _run_subprocess(init_cmd, target_dir, output, f"ERROR INITIALIZING GIT-DRS for {gh_repo_url}"):
            return False

        for file in files:
            pull_cmd = ["git-lfs", "pull", "-I", file["filePath"]]
            if not _run_subprocess(pull_cmd, target_dir, output, f"ERROR PULLING FILE {file['filePath']}"):
                return False
            output['logs'].append(f"DOWNLOADED {file['filePath']}")

            mv_cmd = ["mv", os.path.join(target_dir, file["filePath"]), str(dest_dir)]
            if not _run_subprocess(mv_cmd, target_dir, output, f"ERROR MOVING {file['filePath']}"):
                return False

        return True

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _download_and_unzip: {e}", Exception)


def _load_all(
    hostname: str,
    program: str,
    project: str,
    output: Dict[str, Any],
    file_path: pathlib.Path,
    work_path: str
) -> bool:
    """Load data into graph, flat, and FHIR stores."""
    project_id = f"{program}-{project}"
    work_path = pathlib.Path(work_path)
    db_path = work_path / "local_fhir.db"

    try:
        grip_delete(
            hostname,
            graph_name=_get_env_var('GRIP_GRAPH_NAME'),
            project_id=project_id,
            output=output,
            access_token=_get_env_var('ACCESS_TOKEN')
        )

        for file in file_path.rglob('*'):
            if file.suffix in ['.ndjson', '.json']:
                status = bulk_load_raw(
                    hostname,
                    _get_env_var('GRIP_GRAPH_NAME'),
                    project_id,
                    str(file),
                    output,
                    _get_env_var('ACCESS_TOKEN'),
                )
                output["logs"].append(status)
                logging.info(f"bulk_load_raw return status {status}")
                if status["status"] != 200:
                    raise Exception(f"Critical Error load of file {file} returned non 200 status {status['status']}")

        if not work_path.exists():
            raise ValueError(f"Directory {work_path} does not exist.")
        db_path.unlink(missing_ok=True)

        logging.info("loading sqlite db...")
        output["logs"].append("loading sqlite db...")
        db = LocalFHIRDatabase(db_name=db_path)
        db.bulk_insert_data(
            resources=get_project_data(
                hostname,
                _get_env_var('GRIP_GRAPH_NAME'),
                project_id,
                output,
                _get_env_var('ACCESS_TOKEN'),
                1024*1024)
        )

        index_generator_dict = {
            'researchsubject': db.flattened_research_subjects,
            'specimen': db.flattened_specimens,
            'file': db.flattened_document_references,
            "medicationadministration": db.flattened_medication_administrations,
            "groupmember": db.flattened_group_members,
        }

        logging.info("loading opensearch...")
        output["logs"].append("loading opensearch...")
        for index in index_generator_dict:
            meta_flat_delete(project_id=project_id, index=index)
            load_flat(
                project_id=project_id,
                index=index,
                generator=index_generator_dict[index](),
                limit=None,
                elastic_url=DEFAULT_ELASTIC,
                output_path=None
            )

    except OpenSearchException as e:
        _handle_error(output, f"An ElasticSearch Exception occurred: {str(e)}\n{traceback.format_exc()}", OpenSearchException)
    except Exception as e:
        _handle_error(output, f"An Exception Occurred: {str(e)}\n{traceback.format_exc()}", Exception)

    output['logs'].append(f"Loaded {project_id}")
    return True

def _empty_project(hostname: str, output: Dict[str, Any], program: str, project: str) -> None:
    """Clear out graph and flat metadata for project."""
    project_id = f"{program}-{project}"
    try:
        grip_delete(
            hostname,
            graph_name=_get_env_var('GRIP_GRAPH_NAME'),
            project_id=project_id,
            output=output,
            access_token=_get_env_var('ACCESS_TOKEN'),
        )
        output['logs'].append(f"EMPTIED graph for {project_id}")
        for index in ["researchsubject", "specimen", "file"]:
            meta_flat_delete(project_id=project_id, index=index)
        output['logs'].append(f"EMPTIED flat for {project_id}")
    except Exception as e:
        _handle_error(output, f"An Exception Occurred emptying project {project_id}: {str(e)}\n{traceback.format_exc()}", Exception)


def _run_subprocess(cmd: List[str], cwd: str, output: Dict[str, Any], error_msg: str) -> bool:
    """Run subprocess command and handle errors."""
    try:
        result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
        logging.info(f"Successfully ran command: {' '.join(cmd)}")
        if result.stdout:
            output['logs'].append(f"STDOUT: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        detailed_error = f"{error_msg}. Command failed with return code {e.returncode}."
        if e.stderr:
            detailed_error += f"\nGit Error Details:\n{e.stderr.strip()}"
        _handle_error(output, detailed_error, subprocess.CalledProcessError)
    except FileNotFoundError:
        _handle_error(output, f"Command not found: {cmd[0]}", FileNotFoundError)
    except Exception as e:
        _handle_error(output, f"An unexpected error occurred: {e}", Exception)
    return False


def _write_output_to_client(output):
    '''
    formats output as json to stdout so it is passed back to the client,
    most importantly to display relevant logs from the job erroring out
    '''
    logging.info(f"[out] {json.dumps(output, separators=(',', ':'))}")


def _handle_error(output: Dict[str, Any], message: str, exception_type: type = Exception):
    """A helper function to log errors, update output, and raise an exception."""
    logging.error(message)
    output['logs'].append(message)
    _write_output_to_client(output)
    raise exception_type(message)

def _validate_and_extract_input(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and extract required fields from the input_data dictionary."""
    required_fields = [
        'ghUserName',
        'ghToken',
        'ghCommitHash',
        'ghRepoUrl',
        'bucketName',
        'profile',
        'APIEndpoint',
        'files'
    ]

    for field in required_fields:
        if field not in input_data or not input_data[field]:
            raise ValueError(f"input data must contain a `{field}`")

    files = input_data['files']
    if not isinstance(files, list):
        raise TypeError("`files` must be a list")

    if len(files) > 0:
        commit_fields = ['filePath', 'fileTitle']
        for file in files:
            if not isinstance(file, dict):
                raise TypeError("each item in `files` must be a dictionary")
            for field in commit_fields:
                if field not in file or not file[field]:
                    raise ValueError(f"file data must contain a `{field}`")
    return input_data


def translate_to_fhir(drs_record: Dict[str, Any], project_id: str, hostname: str, research_study_id: str) -> DocumentReference:
    """Translate a DRS record into a FHIR DocumentReference object."""
    did = drs_record.get("did")
    file_name = drs_record.get("file_name")
    size = drs_record.get("size")
    hashes = drs_record.get("hashes", {})
    created_date = format_fhir_datetime(drs_record.get("created_date"))
    updated_date = format_fhir_datetime(drs_record.get("updated_date"))
    urls = drs_record.get("urls", [])

    identifier = Identifier(use="official", system=f"{hostname}/{project_id}", value=did)
    extensions = [Extension(url=f"{hostname}/fhir/StructureDefinition/checksum-{hash_type}", valueString=hash_value) for hash_type, hash_value in hashes.items()]
    attachment_obj = Attachment(creation=created_date, size=size, title=file_name, extension=extensions or None, url=urls[0] if urls else None)
    content = DocumentReferenceContent(attachment=attachment_obj)

    return DocumentReference(
        id=did, status="current", docStatus="final", date=updated_date, identifier=[identifier], content=[content],subject={"reference": f"{RESEARCH_STUDY}/{research_study_id}"}
    )


def get_resource_files(fhir_directory: str, resource_type: str) -> List[str]:
    """Search directory for NDJSON files of the specified FHIR resource type."""
    resource_files = []
    for filename in os.listdir(fhir_directory):
        if not filename.endswith(".ndjson"):
            continue
        file_path = os.path.join(fhir_directory, filename)
        try:
            with open(file_path, 'r') as f:
                if first_line := f.readline().strip():
                    if orjson.loads(first_line.encode()).get("resourceType") == resource_type:
                        resource_files.append(file_path)
        except (IOError, orjson.JSONDecodeError):
            continue
    return resource_files


def _put(hostname: str,
         input_data: Dict[str, Any],
         output: Dict[str, Any],
         program: str,
         project: str,
         user: Dict[str, Any]):
    """
    Import data from a bucket to the graph, flat, and fhir stores.

    Args:
        hostname (str): The hostname for the API endpoint.
        input_data (Dict[str, Any]): The input data containing user and file information.
        output (Dict[str, Any]): A dictionary to store logs and output files.
        program (str): The program name.
        project (str): The project name.
        user (Dict[str, Any]): The user information.
    """
    try:
        if not _can_create(output, program, project, user):
            error_msg = (f"ERROR 401: No permissions to create project {project} on program {program}. "
                         "You can view your project-level permissions with g3t ping")
            _handle_error(output, error_msg, Exception)

        validated_data = _validate_and_extract_input(input_data)

        load_path = pathlib.Path(f"/root/repo/{project}")
        load_path.mkdir(parents=True, exist_ok=True)

        success = _download_and_unzip(
            gh_username=validated_data['ghUserName'],
            gh_token=validated_data['ghToken'],
            gh_repo_url=validated_data['ghRepoUrl'],
            gh_commit_hash=validated_data['ghCommitHash'],
            files=validated_data['file'],
            bucket=validated_data['bucketName'],
            profile=validated_data['profile'],
            api_endpoint=validated_data['APIEndpoint'],
            project_id=f"{program}-{project}",
            output=output,
            dest_dir=load_path
        )

        _apply_indexd_server_info(program, project, validated_data['ghRepoUrl'], output)

        if success:
            found_files = [str(p) for p in load_path.glob('*')]
            output['files'].extend(found_files)
            logging.info(f"Found files: {found_files}")
            _load_all(hostname, program, project, output, load_path, "work")

        if load_path.exists():
            #shutil.rmtree(load_path)
            logging.info(f"Cleaned up directory: {load_path}")

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _put: {e}", Exception)


def get_research_study(fhir_directory: str, program: str, project: str) -> Optional[ResearchStudy]:
    """Load existing ResearchStudy from NDJSON or create a new one."""
    research_study_files = get_resource_files(fhir_directory, RESEARCH_STUDY)
    for research_study_file in research_study_files:
        try:
            with open(research_study_file, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    record_dict = orjson.loads(line.encode())
                    if record_dict.get("resourceType") == RESEARCH_STUDY:
                        return ResearchStudy.parse_obj(record_dict)
        except (IOError, orjson.JSONDecodeError, ValueError) as e:
            logging.error(f"Error reading ResearchStudy file {research_study_file}: {e}. Skipping file.")

    # Create new ResearchStudy if none exists
    new_id = str(uuid.uuid4())
    skeleton = {
        "description": f"Skeleton ResearchStudy for {program}-{project}",
        "id": new_id,
        "identifier": [
            {
                "system": f"https://caliper-training.ohsu.edu/{program}-{project}",
                "use": "official",
                "value": f"{program}-{project}"
            }
        ],
        "resourceType": RESEARCH_STUDY,
        "status": "active"
    }
    research_study = ResearchStudy.parse_obj(skeleton)

    # Save new ResearchStudy to a new file
    new_research_study_file = os.path.join(fhir_directory, f"{RESEARCH_STUDY}.ndjson")
    try:
        with open(new_research_study_file, 'wb') as f:
            f.write(orjson.dumps(skeleton) + b"\n")
        logging.info(f"Created new ResearchStudy at {new_research_study_file} with ID {new_id}")
    except IOError as e:
        logging.error(f"Error writing ResearchStudy file {new_research_study_file}: {e}")

    return research_study


def _process_drs_records_and_update_fhir(drs_records_file: str, fhir_directory: str, program: str, project: str) -> None:
    """Process DRS records and update FHIR NDJSON files with UPSERT operation."""
    # Load or create ResearchStudy
    research_study = get_research_study(fhir_directory, program, project)
    research_study_id = research_study.id if research_study else "4aedbb51-bf36-5fa2-b676-1eac919c284b"

    doc_ref_files = get_resource_files(fhir_directory, DOCUMENT_REFERENCE) or [os.path.join(fhir_directory, f"{DOCUMENT_REFERENCE}.ndjson")]
    if not doc_ref_files:
        logging.info(f"No DocumentReference file(s) found. Creating new file at {doc_ref_files[0]}")

    existing_fhir_records = {}
    record_to_file_map = {}
    for file_path in doc_ref_files:
        if not os.path.exists(file_path):
            continue
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record_dict = orjson.loads(line.encode())
                        record = DocumentReference.parse_obj(record_dict)
                        if record.id:
                            existing_fhir_records[record.id] = record
                            record_to_file_map[record.id] = file_path
                    except ValueError as e:
                        logging.error(f"Invalid FHIR record in {file_path}: {e}. Skipping record.")
        except (IOError, orjson.JSONDecodeError) as e:
            logging.error(f"Error reading FHIR file {file_path}: {e}. Skipping file.")

    try:
        with open(drs_records_file, 'r') as drs_file:
            hostname = f"https://{_get_env_var('GEN3_HOSTNAME')}"
            for line in drs_file:
                try:
                    drs_record = orjson.loads(line.encode())
                    fhir_record = translate_to_fhir(drs_record, f"{program}-{project}", hostname, research_study_id)
                    record_id = fhir_record.id

                    if record_id in existing_fhir_records:
                        existing = existing_fhir_records[record_id]
                        existing.status = fhir_record.status
                        existing.docStatus = fhir_record.docStatus
                        existing.date = fhir_record.date
                        existing.identifier = fhir_record.identifier
                        if existing.content and fhir_record.content:
                            existing_attachment = existing.content[0].attachment
                            new_attachment = fhir_record.content[0].attachment
                            existing_attachment.creation = new_attachment.creation
                            existing_attachment.size = new_attachment.size
                            existing_attachment.title = new_attachment.title
                            existing_attachment.extension = new_attachment.extension or None
                            existing_attachment.url = new_attachment.url or None
                        else:
                            existing.content = fhir_record.content
                        existing.subject = {"reference": f"{RESEARCH_STUDY}/{research_study_id}"}
                        logging.info(f"Merged and updated record: {record_id}")
                    else:
                        existing_fhir_records[record_id] = fhir_record
                        record_to_file_map[record_id] = doc_ref_files[0]
                        logging.info(f"Added new record: {record_id}")
                except orjson.JSONDecodeError as e:
                    logging.error(f"Error decoding JSON from DRS file: {e}")
                except ValueError as e:
                    logging.error(f"Invalid FHIR data from DRS record: {e}. Skipping record.")
    except IOError as e:
        logging.error(f"Error reading DRS records file: {e}")
        return

    try:
        file_contents = {path: [] for path in doc_ref_files}
        for record_id, record in existing_fhir_records.items():
            file_contents[record_to_file_map.get(record_id, doc_ref_files[0])].append(record)

        for file_path, records in file_contents.items():
            with open(file_path, 'wb') as f:
                for record in records:
                    if not isinstance(record, DocumentReference):
                        logging.error(f"Error: Record with id {record.id} is not a DocumentReference. Skipping.")
                        continue
                    try:
                        f.write(orjson.dumps(record.dict(exclude_none=True)) + b"\n")
                    except AttributeError as e:
                        logging.error(f"Error serializing record with id {record.id}: {e}. Skipping.")
        logging.info("Finished writing all records to files.")
    except IOError as e:
        logging.error(f"Error writing to FHIR files: {e}")


def format_fhir_datetime(dt_str):
    if not dt_str:
        return None
    if '.' in dt_str:
        base, micro = dt_str.split('.')
        milli = micro[:3]
        return f"{base}.{milli}+00:00"
    else:
        return f"{dt_str}+00:00"


def _apply_indexd_server_info(program, project, gh_repo_url, output):
    """Query Indexd for all File objects that belong to a given project
        UPSERT DocumentReference rows that match the returned indexd IDs

        In cases where the record exists but doesn't contain the indexd related information,
        populate the record with the indexd file information, and keep the existing information

    Args:
        project_id: the program-project string

    """

    repo_name = pathlib.Path(gh_repo_url).stem
    target_dir = os.path.join(os.getcwd(), repo_name)
    load_path = pathlib.Path(f"/root/repo/{project}")

    pull_cmd = ["git-drs", "list-project", f"{program}-{project}", "-o", "OBJ.ndjson"]
    if not _run_subprocess(pull_cmd, target_dir, output, f"ERROR LISTING IDEXD RECORDS FOR PROJECT {program}-{project}"):
        return False

    _process_drs_records_and_update_fhir(os.path.join(target_dir, "OBJ.ndjson"), load_path, program, project)



def main() -> None:
    """Main entry point for FHIR import/export."""
    token = _get_env_var('ACCESS_TOKEN')
    auth = _auth(token)
    logging.info("[out] authorized successfully")

    hostname = f"https://{_get_env_var('GEN3_HOSTNAME')}"
    logging.info(f"[out] HOSTNAME: {hostname}")
    logging.info("[out] retrieving user info...")

    user = _get_user(auth)
    output = {'user': user['email'], 'files': [], 'logs': []}
    input_data = _parse_input_data()
    _write_output_to_client(input_data)
    program, project = _get_program_project(input_data)

    method = input_data.get("method")
    if not method:
        raise ValueError("input data must contain a `method`")

    if method.lower() == 'put':
        _put(hostname, input_data, output, program, project, user)
    elif method.lower() == 'delete':
        _empty_project(hostname, output, program, project)
    else:
        _handle_error(output, f"unknown method {method}", ValueError)

    _write_output_to_client(output)

if __name__ == '__main__':
    main()



def _can_read(output: dict,
              program: str,
              project: str,
              user: dict) -> bool:
    """
    For checking read permissions. Not currently being used
    Check if user can read a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        project: project Gen3 (program-)project
        user: user dict from arborist (aka profile)
    """

    can_read = True

    required_resources = [
        f"/programs/{program}",
        f"/programs/{program}/projects"
    ]
    for required_resource in required_resources:
        if required_resource not in user['resources']:
            output['logs'].append(f"{required_resource} not found in user resources")
            can_read = False
        else:
            output['logs'].append(f"HAS RESOURCE {required_resource}")

    required_services = [
        f"/programs/{program}/projects/{project}"
    ]
    for required_service in required_services:
        if required_service not in user['authz']:
            output['logs'].append(f"{required_service} not found in user authz")
            can_read = False
        else:
            if {'method': 'read-storage', 'service': '*'} not in user['authz'][required_service]:
                output['logs'].append(f"read-storage not found in user authz for {required_service}")
                can_read = False
            else:
                output['logs'].append(f"HAS SERVICE read-storage on resource {required_service}")

    return can_read
