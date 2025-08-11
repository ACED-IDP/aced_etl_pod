import json
import logging
import os
import pathlib
import shutil
import sys
import traceback
import subprocess
import orjson

from aced_submission.meta_flat_load import DEFAULT_ELASTIC, load_flat
from aced_submission.meta_flat_load import delete as meta_flat_delete
from aced_submission.grip_load import bulk_load_raw, get_project_data, \
    delete_project as grip_delete
from opensearchpy import OpenSearchException
from gen3.auth import Gen3Auth
from gen3_tracker.meta.dataframer import LocalFHIRDatabase
from typing import List, Dict, Any, Tuple, Optional


from fhir.resources.documentreference import DocumentReference
from fhir.resources.documentreference import DocumentReferenceContent
from fhir.resources.attachment import Attachment
from fhir.resources.identifier import Identifier
from fhir.resources.extension import Extension

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


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
    required_fields = {
        'ghUserName': "input data must contain a `ghUserName`",
        'ghToken': "input data must contain a `ghToken`",
        'ghCommitHash': "input data must contain a `ghCommitHash`",
        'ghRepoUrl': "input data must contain a `ghRepoUrl`",
        'bucketName': "input data must contain a `bucketName`",
        'profile': "input data must contain a `profile`",
        'APIEndpoint': "input data must contain a `APIEndpoint`",
    }

    for field, error_msg in required_fields.items():
        if field not in input_data or not input_data[field]:
            raise ValueError(error_msg)

    files = input_data['file']
    if len(files) > 0:
        commit_fields = {
            'filePath': "a file must contain a `filePath`",
            'fileTitle': "a file must contain a `fileTitle`"
        }

        for file in files:
            for field, error_msg in commit_fields.items():
                if field not in file or not file[field]:
                    raise ValueError(error_msg)
    return input_data


def translate_to_fhir(drs_record: Dict[str, Any]) -> DocumentReference:
    """Translate a DRS record into a FHIR DocumentReference object."""
    did = drs_record.get("did")
    file_name = drs_record.get("file_name")
    size = drs_record.get("size")
    hashes = drs_record.get("hashes", {})
    created_date = format_fhir_datetime(drs_record.get("created_date"))
    updated_date = format_fhir_datetime(drs_record.get("updated_date"))
    urls = drs_record.get("urls", [])

    identifier = Identifier(use="official", system="urn:drs:did", value=did)
    extensions = [Extension(url=f"http://hl7.org/fhir/StructureDefinition/checksum-{hash_type}", valueString=hash_value) for hash_type, hash_value in hashes.items()]
    attachment_obj = Attachment(creation=created_date, size=size, title=file_name, extension=extensions or None, url=urls[0] if urls else None)
    content = DocumentReferenceContent(attachment=attachment_obj)

    return DocumentReference(
        id=did, status="current", docStatus="final", date=updated_date, identifier=[identifier], content=[content]
    )


def get_document_reference_files(fhir_directory):
    """
    Searches the directory for all NDJSON files of type "DocumentReference"
    and returns a list of their paths.
    """
    doc_ref_files = []
    for filename in os.listdir(fhir_directory):
        if filename.endswith(".ndjson"):
            file_path = os.path.join(fhir_directory, filename)
            try:
                with open(file_path, 'r') as f:
                    first_line = f.readline()
                    if not first_line:
                        continue
                    first_record = orjson.loads(first_line.encode())
                    if first_record.get("resourceType") == "DocumentReference":
                        doc_ref_files.append(file_path)
            except (IOError, orjson.JSONDecodeError):
                continue
    return doc_ref_files


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
            shutil.rmtree(load_path)
            logging.info(f"Cleaned up directory: {load_path}")

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _put: {e}", Exception)


def _process_drs_records_and_update_fhir(drs_records_file, fhir_directory):
    """
    Main processing logic. Reads DRS records and updates a single FHIR NDJSON file
    with an UPSERT-like operation, preserving existing FHIR fields and updating only
    file-related metadata.
    """
    # 1. Find all target FHIR files
    doc_ref_files = get_document_reference_files(fhir_directory)
    if not doc_ref_files:
        fhir_file_path = os.path.join(fhir_directory, "DocumentReference.ndjson")
        doc_ref_files = [fhir_file_path]
        sys.stdout.write(f"No DocumentReference file(s) found. Creating new file at {fhir_file_path}\n")

    # 2. Load all existing FHIR records from all files into a single collection
    existing_fhir_records = {}
    record_to_file_map = {}
    for file_path in doc_ref_files:
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    for line in f:
                        if not line.strip():
                            continue
                        record_dict = orjson.loads(line.encode())
                        # Validate and create FHIR DocumentReference object
                        try:
                            record = DocumentReference.parse_obj(record_dict)
                            record_id = record.id
                            if record_id:
                                existing_fhir_records[record_id] = record
                                record_to_file_map[record_id] = file_path
                        except ValueError as e:
                            sys.stderr.write(f"Invalid FHIR record in {file_path}: {e}. Skipping record.\n")
                            continue
            except (IOError, orjson.JSONDecodeError) as e:
                sys.stderr.write(f"Error reading existing FHIR file {file_path}: {e}. Skipping this file.\n")

    # 3. Process each new DRS record and merge/add
    try:
        with open(drs_records_file, 'r') as drs_file:
            for line in drs_file:
                try:
                    drs_record = orjson.loads(line.encode())
                    fhir_document_reference = translate_to_fhir(drs_record)
                    record_id = fhir_document_reference.id

                    if record_id in existing_fhir_records:
                        # Update existing record with file-related metadata
                        existing = existing_fhir_records[record_id]
                        # Update top-level fields
                        existing.status = fhir_document_reference.status
                        existing.docStatus = fhir_document_reference.docStatus
                        existing.date = fhir_document_reference.date
                        existing.identifier = fhir_document_reference.identifier

                        # Update content.attachment fields
                        if existing.content and fhir_document_reference.content:
                            existing_attachment = existing.content[0].attachment
                            new_attachment = fhir_document_reference.content[0].attachment
                            existing_attachment.creation = new_attachment.creation
                            existing_attachment.size = new_attachment.size
                            existing_attachment.title = new_attachment.title
                            if new_attachment.extension:
                                existing_attachment.extension = new_attachment.extension
                            else:
                                existing_attachment.extension = None
                            if new_attachment.url:
                                existing_attachment.url = new_attachment.url
                            else:
                                existing_attachment.url = None
                        else:
                            # If no content exists, set it
                            existing.content = fhir_document_reference.content

                        sys.stdout.write(f"Merged and updated record: {record_id}\n")
                    else:
                        # Add new record
                        existing_fhir_records[record_id] = fhir_document_reference
                        record_to_file_map[record_id] = doc_ref_files[0]
                        sys.stdout.write(f"Added new record: {record_id}\n")
                except orjson.JSONDecodeError as e:
                    sys.stderr.write(f"Error decoding JSON from DRS file: {e}\n")
                    continue
                except ValueError as e:
                    sys.stderr.write(f"Invalid FHIR data from DRS record: {e}. Skipping record.\n")
                    continue
    except IOError as e:
        sys.stderr.write(f"Error reading DRS records file: {e}\n")
        return

    # 4. Write the entire updated collection back to the files
    try:
        file_contents = {path: [] for path in doc_ref_files}
        for record_id, record in existing_fhir_records.items():
            target_file = record_to_file_map.get(record_id, doc_ref_files[0])
            file_contents[target_file].append(record)

        for file_path, records in file_contents.items():
            with open(file_path, 'wb') as f:
                for record in records:
                    # Ensure record is a DocumentReference object
                    if not isinstance(record, DocumentReference):
                        sys.stderr.write(f"Error: Record with id {record.id} is not a DocumentReference object. Skipping.\n")
                        continue
                    # Dump to dict excluding unset fields, then to json
                    try:
                        record_dict = record.dict(exclude_none=True)
                        f.write(orjson.dumps(record_dict) + b"\n")
                    except AttributeError as e:
                        sys.stderr.write(f"Error serializing record with id {record.id}: {e}. Skipping.\n")
                        continue
        sys.stdout.write("Finished writing all records to files.\n")
    except IOError as e:
        sys.stderr.write(f"Error writing to FHIR files: {e}\n")

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

    _process_drs_records_and_update_fhir(os.path.join(target_dir, "OBJ.ndjson"), load_path)



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
