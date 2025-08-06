import json
import logging
import os
import pathlib
import shutil
import sys
import traceback
import subprocess

from aced_submission.meta_flat_load import DEFAULT_ELASTIC, load_flat
from aced_submission.meta_flat_load import delete as meta_flat_delete
from aced_submission.grip_load import bulk_load_raw, get_project_data, \
    delete_project as grip_delete
from opensearchpy import OpenSearchException
from gen3.auth import Gen3Auth
from gen3_tracker.meta.dataframer import LocalFHIRDatabase
from typing import List, Dict, Any

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _get_token() -> str | None:
    """Get ACCESS_TOKEN from environment"""
    return os.environ.get('ACCESS_TOKEN', None)


def _get_hostname() -> str | None:
    """Get host name from environment"""
    return os.environ.get('GEN3_HOSTNAME', None)


def _get_graphName() -> str | None:
    """Get the Grip graph name that data is to be loaded to"""
    return os.environ.get("GRIP_GRAPH_NAME", None)


def _auth(access_token) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN"""
    if access_token:
        # use access token from environment (set by sower)
        return Gen3Auth(refresh_file=f"accesstoken:///{access_token}")
    # no access token, use refresh token set in default ~/.gen3/credentials.json location
    return Gen3Auth()


def _user(auth: Gen3Auth) -> dict:
    """Get user info from arborist"""
    return auth.curl('/user/user').json()


def _input_data() -> dict:
    """Get input data"""
    assert 'INPUT_DATA' in os.environ, "INPUT_DATA not found in environment"
    return json.loads(os.environ['INPUT_DATA'])


def _get_program_project(input_data: dict) -> tuple:
    """Get program and project from input_data"""
    assert 'projectId' in input_data, "project_id not found in INPUT_DATA"
    assert '-' in input_data['projectId'], 'project_id must be in the format <program>-<project>'
    return input_data['projectId'].split('-')


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


def _can_read(output: dict,
              program: str,
              project: str,
              user: dict) -> bool:
    """Check if user can read a project in the given program.

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


def _download_and_unzip(username: str,
                        gh_token: str,
                        file_path: str,
                        repo_url: str,
                        bucket: str,
                        profile: str,
                        api_endpoint: str,
                        project: str,
                        output: Dict[str, Any],
                        dest_dir: pathlib.Path) -> bool | None:
    """
    Download and unzip an object from Git LFS to a destination directory.
    """
    try:
        repo_name = pathlib.Path(repo_url).stem
        target_dir = os.path.join(os.getcwd(), repo_name)

        # Git clone
        clone_url = f"https://{username}:{gh_token}@{repo_url}"
        logging.info(f"clone URL: {clone_url}")
        if not _run_subprocess(["git", "clone", clone_url], os.getcwd(), output, f"ERROR CLONING {repo_url}"):
            return False

        # Git LFS init
        token = _get_token()
        init_cmd = ["git-drs", "init", "--bucket", bucket, "--token", token, "--profile", profile, "--project", project, "--url", api_endpoint]
        if not _run_subprocess(init_cmd, target_dir, output, f"ERROR INITIALIZING GIT-DRS for {repo_url}"):
            return False

        # Git LFS pull
        pull_cmd = ["git-lfs", "pull", "-I", file_path]
        if not _run_subprocess(pull_cmd, target_dir, output, f"ERROR PULLING FILE {file_path}"):
            return False

        output['logs'].append(f"DOWNLOADED {file_path}")

        # Unzip the file
        unzip_cmd = ["unzip", "-o", "-j", os.path.join(target_dir, file_path), "-d", str(dest_dir)]
        if not _run_subprocess(unzip_cmd, target_dir, output, f"ERROR UNZIPPING {file_path}"):
            return False

        output['logs'].append(f"UNZIPPED {dest_dir}")
        return True

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _download_and_unzip: {e}", Exception)



def _load_all(hostname,
              program: str,
              project: str,
              output: dict,
              file_path: str,
              work_path: str) -> bool:

    logs = None
    try:
        for file in pathlib.Path(file_path).rglob('*'):
            if file.suffix in ['.ndjson', '.json']:
                # output dictionary is capturing logs from this function
                status = bulk_load_raw(hostname, _get_graphName(),
                    f"{program}-{project}", str(file), output, _get_token())
                output["logs"].append(status)
                print(status)
                if status["status"] != 200:
                    raise Exception(f"Critical Error load of file {file} returned non 200 status {status['status']}")

        assert pathlib.Path(work_path).exists(), f"Directory {work_path} does not exist."
        work_path = pathlib.Path(work_path)
        db_path = (work_path / "local_fhir.db")
        db_path.unlink(missing_ok=True)

        print("loading sqlite db...")
        output["logs"].append("loading sqlite db...")

        db = LocalFHIRDatabase(db_name=db_path)
        db.bulk_insert_data(resources=get_project_data(hostname, _get_graphName(), f"{program}-{project}", output, _get_token(), 1024*1024))

        index_generator_dict = {
            'researchsubject': db.flattened_research_subjects,
            'specimen': db.flattened_specimens,
            'file': db.flattened_document_references,
            "medicationadministration": db.flattened_medication_administrations,
            "groupmember": db.flattened_group_members,
        }

        print("loading opensearch...")
        output["logs"].append("loading opensearch...")

        # To ensure differences in the dataframer versions do not conflict, clear the project, and reload the project.
        for index in index_generator_dict.keys():
            meta_flat_delete(project_id=f"{program}-{project}", index=index)

        for index, generator in index_generator_dict.items():
            load_flat(project_id=f"{program}-{project}", index=index,
                      generator=generator(),
                      limit=None, elastic_url=DEFAULT_ELASTIC,
                      output_path=None)

    # when making changes to Elasticsearch
    except OpenSearchException as e:
        output['logs'].append(f"An ElasticSearch Exception occurred: {str(e)}")
        tb = traceback.format_exc()
        print("TRACEBACK: ", tb)
        print("OpenSearchException: ", str(e))
        output['logs'].append(tb)
        if logs is not None:
            output['logs'].extend(logs)
        _write_output_to_client(output)
        raise

    # all other exceptions
    except Exception as e:
        output['logs'].append(f"An Exception Occurred: {str(e)}")
        tb = traceback.format_exc()
        print("TRACEBACK: ", tb)
        print("Exception: ", str(e))
        output['logs'].append(tb)
        if logs is not None:
            output['logs'].extend(logs)
        _write_output_to_client(output)
        raise

    output['logs'].append(f"Loaded {program}-{project}")
    if logs is not None:
        output['logs'].extend(logs)
    return True


def _run_subprocess(cmd: List[str], cwd: str, output: Dict[str, Any], descriptive_error: str) -> bool:
    """Helper function to run subprocess commands and handle errors with detailed messages."""
    try:
        # Use subprocess.run with check=True and capture_output=True
        # This will automatically raise a CalledProcessError on non-zero exit codes.
        result = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True  # Decodes stdout and stderr as strings
        )
        logging.info(f"Successfully ran command: {' '.join(cmd)}")
        if result.stdout:
            logging.debug(f"STDOUT: {result.stdout.strip()}")
            output['logs'].append(f"STDOUT: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        # The key improvement: include the stderr from the failed command.
        detailed_error_msg = f"{descriptive_error}. Command failed with return code {e.returncode}."
        if e.stderr:
            detailed_error_msg += f"\nGit Error Details:\n{e.stderr.strip()}"

        # Raise a custom exception or a more informative one
        _handle_error(output, detailed_error_msg, subprocess.CalledProcessError)
        return False
    except FileNotFoundError:
        _handle_error(output, f"Command not found: {cmd[0]}", FileNotFoundError)
    except Exception as e:
        _handle_error(output, f"An unexpected error occurred: {e}", Exception)


def _empty_project(hostname,
                   output: dict,
                   program: str,
                   project: str,
                   user: dict,
                   config_path: str | None = None):
    """Clear out graph and flat metadata for project """
    # check permissions
    try:
        grip_delete(hostname, graph_name=_get_graphName(),
                    project_id=f"{program}-{project}",
                    output=output, access_token=_get_token())
        output['logs'].append(f"EMPTIED graph for {program}-{project}")

        for index in ["researchsubject", "specimen", "file"]:
            meta_flat_delete(project_id=f"{program}-{project}", index=index)
        output['logs'].append(f"EMPTIED flat for {program}-{project}")

    except Exception as e:
        output['logs'].append(f"An Exception Occurred emptying project {program}-{project}: {str(e)}")
        tb = traceback.format_exc()
        output['logs'].append(tb)
        _write_output_to_client(output)
        raise


def main():
    token = _get_token()
    auth = _auth(token)
    hostname = "https://" + str(_get_hostname())
    print("[out] HOSTNAME: ", hostname)


    print("[out] authorized successfully")
    print("[out] retrieving user info...")
    user = _user(auth)

    output = {'user': user['email'], 'files': [], 'logs': []}
    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller

    # output['env'] = {k: v for k, v in os.environ.items()}

    input_data = _input_data()
    _write_output_to_client(input_data)
    program, project = _get_program_project(input_data)

    method = input_data.get("method", None)
    assert method, "input data must contain a `method`"

    if method.lower() == 'put':
        # read from bucket, write to fhir store
        _put(hostname, input_data, output, program, project, user)
    elif method.lower() == 'delete':
        _empty_project(hostname, output, program, project, user,
                       config_path="config.yaml")
    else:
        raise Exception(f"unknown method {method}")

    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller
    _write_output_to_client(output)


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
        # Check permissions and handle early exit
        if not _can_create(output, program, project, user):
            error_msg = (f"ERROR 401: No permissions to create project {project} on program {program}. "
                         "You can view your project-level permissions with g3t ping")
            _handle_error(output, error_msg, Exception)

        # Use a separate function to validate and extract input data
        validated_data = _validate_and_extract_input(input_data)

        # Use descriptive variable names
        username = validated_data['ghUserName']
        gh_token = validated_data['ghToken']
        bucket_name = validated_data['bucketName']
        profile = validated_data['profile']
        api_endpoint = validated_data['APIEndpoint']

        # Extract commit details
        commit = validated_data['push']['commits'][-1]
        file_path = commit['filePath']
        repo_url = commit['repoUrl']

        # Define paths
        unzip_path = pathlib.Path(f"/root/repo/{project}/snapshot")
        unzip_path.mkdir(parents=True, exist_ok=True)
        repo_path = pathlib.Path(f"/root/repo/{project}")

        # Download and unzip the data
        success = _download_and_unzip(
            username=username,
            gh_token=gh_token,
            file_path=file_path,
            repo_url=repo_url,
            bucket=bucket_name,
            profile=profile,
            api_endpoint=api_endpoint,
            project=f"{program}-{project}",
            output=output,
            dest_dir=unzip_path
        )

        if success:
            # Log files found
            found_files = [str(p) for p in unzip_path.glob('*')]
            output['files'].extend(found_files)
            logging.info(f"Found files: {found_files}")

            # Load the data
            _load_all(hostname, program, project, output, unzip_path, "work")

        # Clean up the repository directory
        if repo_path.exists():
            shutil.rmtree(repo_path)
            logging.info(f"Cleaned up directory: {repo_path}")

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _put: {e}", Exception)



def _write_output_to_client(output):
    '''
    formats output as json to stdout so it is passed back to the client,
    most importantly to display relevant logs from the job erroring out
    '''
    print(f"[out] {json.dumps(output, separators=(',', ':'))}")


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
        'bucketName': "input data must contain a `bucketName`",
        'profile': "input data must contain a `profile`",
        'APIEndpoint': "input data must contain a `APIEndpoint`",
        'push': "input data must contain a `push`"
    }

    for field, error_msg in required_fields.items():
        if field not in input_data or not input_data[field]:
            raise ValueError(error_msg)

    # Validate nested commit data
    if 'commits' not in input_data['push'] or not input_data['push']['commits']:
        raise ValueError("`push` data must contain `commits`")

    commit = input_data['push']['commits'][-1]
    commit_fields = {
        'filePath': "commit must contain a `filePath`",
        'repoUrl': "commit must contain a `repoUrl`"
    }

    for field, error_msg in commit_fields.items():
        if field not in commit or not commit[field]:
            raise ValueError(error_msg)
    return input_data

if __name__ == '__main__':
    main()
