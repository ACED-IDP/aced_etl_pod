import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple

import inflection
import requests
from aced_submission.grip_load import bulk_load_raw, get_project_data
from aced_submission.grip_load import delete_project as grip_delete
from aced_submission.meta_flat_load import DEFAULT_ELASTIC, load_flat
from aced_submission.meta_flat_load import delete as meta_flat_delete
from gen3.auth import Gen3Auth
from gen3_tracker.meta.dataframer import LocalFHIRDatabase
from opensearchpy import OpenSearchException

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

# Define the keys in one place at the top of the file
INDEX_NAMES = [
    "research_subject",
    "specimen",
    "document_reference",
    "medication_administration",
    "group_member",
]
META_DIR = "META"
CONFIG_DIR = "CONFIG"

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _get_env_var(key: str, error_msg: Optional[str] = None) -> Optional[str]:
    """Get environment variable or raise error if not found."""
    value = os.environ.get(key)
    if value is None:
        raise ValueError(error_msg)
    return value


def _auth(access_token: Optional[str]) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN or default refresh token."""
    return Gen3Auth(
        refresh_file=f"accesstoken:///{access_token}" if access_token else None
    )


def _get_user(auth: Gen3Auth) -> Dict[str, Any]:
    """Get user info from arborist."""
    return auth.curl("/user/user").json()


def _parse_input_data() -> Dict[str, Any]:
    """Parse INPUT_DATA from environment."""
    return json.loads(_get_env_var("INPUT_DATA", "INPUT_DATA not found in environment"))


def _get_program_project(input_data: Dict[str, Any]) -> Tuple[str, str]:
    """Extract program and project from input_data."""
    project_id = input_data.get("projectId")
    if not project_id or "-" not in project_id:
        raise ValueError("project_id must be in the format <program>-<project>")
    return project_id.split("-")


def _is_guppy_admin(output: dict, program: str, project: str, user: dict) -> bool:
    """
    For checking /guppy_admin permissions.
    Check if user has '*' method on service 'guppy'

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        project: project Gen3 (program-)project
        user: user dict from arborist (aka profile)
    """

    is_guppy_admin = True

    required_resources = [
        "/guppy_admin",
    ]
    for required_resource in required_resources:
        if required_resource not in user["resources"]:
            output["logs"].append(f"{required_resource} not found in user resources")
            is_guppy_admin = False
        else:
            output["logs"].append(f"HAS RESOURCE {required_resource}")

    required_services = ["/guppy_admin"]
    for required_service in required_services:
        if required_service not in user["authz"]:
            output["logs"].append(f"{required_service} not found in user authz")
            is_guppy_admin = False
        else:
            if {"method": "*", "service": "guppy"} not in user["authz"][
                required_service
            ]:
                output["logs"].append(
                    f"'*' method not found in user authz for {required_service}"
                )
                is_guppy_admin = False
            else:
                output["logs"].append(
                    f"HAS SERVICE read-storage on resource {required_service}"
                )

    return is_guppy_admin


def _can_create(output: dict, program: str, project: str, user: dict) -> bool:
    """Check if user can create a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        project: project Gen3 (program-)project
        user: user dict from arborist (aka profile)
    """

    can_create = True

    required_resources = [f"/programs/{program}", f"/programs/{program}/projects"]
    for required_resource in required_resources:
        if required_resource not in user["resources"]:
            output["logs"].append(f"{required_resource} not found in user resources")
            can_create = False
        else:
            output["logs"].append(f"HAS RESOURCE {required_resource}")

    required_services = [f"/programs/{program}/projects/{project}"]
    for required_service in required_services:
        if required_service not in user["authz"]:
            output["logs"].append(f"{required_service} not found in user authz")
            can_create = False
        else:
            if {"method": "create", "service": "*"} not in user["authz"][
                required_service
            ]:
                output["logs"].append(
                    f"create not found in user authz for {required_service}"
                )
                can_create = False
            else:
                output["logs"].append(
                    f"HAS SERVICE create on resource {required_service}"
                )

    return can_create


def _process_config_files(target_dir: str, output: dict, hostname: str) -> bool:
    """
    Processes configuration files located in the CONFIG_DIR of the target directory.
    Assumes LFS files have already been pulled. Reads content and uploads to the ExplorerConfig API.
    """

    config_dir_path = os.path.join(target_dir, CONFIG_DIR)
    config_path = pathlib.Path(config_dir_path)

    if not config_path.exists():
        output["logs"].append(
            f"INFO: Config directory not found at {config_dir_path}. Skipping config processing."
        )
        return True  # Not a critical error if no configs exist

    config_files = [f for f in os.listdir(config_path) if f.endswith(".json")]
    for file in config_files:
        base_name = file[:-5]  # Removes the last 5 characters (".json")
        if base_name.count("-") != 1:
            output["logs"].append(
                f"SKIPPING: File {file} does not contain exactly one hyphen."
            )
            continue

        parts = base_name.split("-", 1)
        program = parts[0]
        project = parts[1]
        if not program or not project:
            output["logs"].append(
                f"SKIPPING: File {file} has an empty program or project name."
            )
            continue

        full_file_path = config_path / file
        try:
            with open(full_file_path, "r") as f:
                # Read the file content to be used as the request body
                file_content = f.read()
        except IOError as err:
            output["logs"].append(f"ERROR READING FILE {file}: {err}")
            return False

        headers = {
            "Authorization": f"bearer {_get_env_var('ACCESS_TOKEN')}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.put(
                f"{hostname}/ExplorerConfig/explorer/{base_name}",
                headers=headers,
                data=file_content,
            )
            response.raise_for_status()
            logging.info(f"ExplorerConfig response: {response}")
            output["logs"].append(f"ExplorerConfig response: {response.status_code}")
        except requests.exceptions.RequestException as err:
            print(f"An unexpected error occurred: {err}")
            output["logs"].append(f"ERROR UPLOADING {file}: {err}")
            return False

    return True


def _download_and_unzip(
    gh_username: str,
    gh_token: str,
    gh_repo_url: str,
    gh_commit_hash: str,
    bucket: str,
    profile: str,
    project_id: str,
    output: Dict[str, Any],
    dest_dir: pathlib.Path,
) -> bool | None:
    """
    Download META and CONFIG objects from Git LFS to the repository directory.
    """
    try:
        repo_name = pathlib.Path(gh_repo_url).stem
        target_dir = os.path.join(os.getcwd(), repo_name)

        clone_url = f"https://{gh_username}:{gh_token}@{gh_repo_url}"
        if not _run_subprocess(
            ["git", "clone", clone_url],
            os.getcwd(),
            output,
            f"ERROR CLONING {gh_repo_url}",
        ):
            return False

        checkout_cmd = ["git", "checkout", gh_commit_hash]
        if not _run_subprocess(
            checkout_cmd,
            target_dir,
            output,
            f"ERROR CHECKING OUT for {gh_repo_url} ON HASH {gh_commit_hash}",
        ):
            return False

        if profile != "origin":
            checkout_cmd = ["git", "remote", "rename", "origin", profile]
            if not _run_subprocess(
                checkout_cmd,
                target_dir,
                output,
                f"ERROR renaming remote for {gh_repo_url} for profile: {profile}",
            ):
                return False

        init_cmd = ["git-drs", "init"]
        if not _run_subprocess(
            init_cmd, target_dir, output, f"ERROR INITIALIZING forge for {gh_repo_url}"
        ):
            return False

        init_cmd = [
            "git-drs",
            "remote",
            "add",
            "gen3",
            profile,
            "--bucket",
            bucket,
            "--token",
            _get_env_var("ACCESS_TOKEN"),
            "--project",
            project_id,
        ]
        if not _run_subprocess(
            init_cmd,
            target_dir,
            output,
            f"ERROR Adding Ref {profile} in git-drs for {gh_repo_url}",
        ):
            return False

        meta_dir_path = os.path.join(target_dir, META_DIR)
        os.makedirs(meta_dir_path, exist_ok=True)
        meta_files_to_pull = [
            os.path.join(META_DIR, f)
            for f in os.listdir(meta_dir_path)
            if f.endswith(".ndjson")
        ]

        if meta_files_to_pull:
            for file in meta_files_to_pull:
                if not _run_subprocess(
                    ["git-lfs", "pull", profile, "-I", file],
                    target_dir,
                    output,
                    "ERROR PULLING META FILES with git-lfs",
                ):
                    return False
                output["logs"].append(f"DOWNLOADED {file}")

        config_dir_path = os.path.join(target_dir, CONFIG_DIR)
        if os.path.exists(config_dir_path):
            config_files_to_pull = [
                os.path.join(CONFIG_DIR, f)
                for f in os.listdir(config_dir_path)
                if f.endswith(".json")
            ]

            if config_files_to_pull:
                for file in config_files_to_pull:
                    if not _run_subprocess(
                        ["git-lfs", "pull", profile, "-I", file],
                        target_dir,
                        output,
                        "ERROR PULLING CONFIG FILES with git-lfs",
                    ):
                        return False
                    output["logs"].append(f"DOWNLOADED {file}")

        return True

    except Exception as e:
        _handle_error(
            output,
            f"An unexpected error occurred in _download_and_unzip: {e}",
            Exception,
        )


def _load_all(
    hostname: str,
    program: str,
    project: str,
    output: Dict[str, Any],
    file_path: pathlib.Path,
    work_path: str,
) -> bool:
    """Load data into graph, flat, and FHIR stores."""
    project_id = f"{program}-{project}"
    work_path = pathlib.Path(work_path)
    db_path = work_path / "local_fhir.db"

    try:
        grip_delete(
            hostname,
            graph_name=_get_env_var("GRIP_GRAPH_NAME"),
            project_id=project_id,
            output=output,
            access_token=_get_env_var("ACCESS_TOKEN"),
        )

        for file in file_path.rglob("*"):
            if file.suffix in [".ndjson", ".json"]:
                status = bulk_load_raw(
                    hostname,
                    _get_env_var("GRIP_GRAPH_NAME"),
                    project_id,
                    str(file),
                    output,
                    _get_env_var("ACCESS_TOKEN"),
                )
                output["logs"].append(status)
                logging.info(f"bulk_load_raw return status {status}")
                if status["status"] != 200:
                    raise Exception(
                        f"Critical Error load of file {file} returned non 200 status {status['status']}"
                    )

        if not work_path.exists():
            raise ValueError(f"Directory {work_path} does not exist.")
        db_path.unlink(missing_ok=True)

        logging.info("loading sqlite db...")
        output["logs"].append("loading sqlite db...")
        db = LocalFHIRDatabase(db_name=db_path)
        db.bulk_insert_data(
            resources=get_project_data(
                hostname,
                _get_env_var("GRIP_GRAPH_NAME"),
                project_id,
                output,
                _get_env_var("ACCESS_TOKEN"),
                1024 * 1024,
            )
        )

        # associate index with generator function, eg "specimen": db.flattened_specimens
        index_generator_dict = {
            # index name needs to match column prefix coming off of the generators otherwise this will fail
            index: getattr(db, f"flattened_{index}s")
            for index in INDEX_NAMES
        }

        # To ensure differences in the dataframer versions do not conflict, clear the project, and reload the project.
        for index in INDEX_NAMES:
            meta_flat_delete(project_id=f"{program}-{project}", index=index)

        for index in INDEX_NAMES:
            generator = index_generator_dict[index]
            prefix = inflection.underscore(index)
            prefixed_generator = (
                {f"{prefix}_{k}": v for k, v in record.items()}
                for record in generator()
            )
            load_flat(
                project_id=f"{program}-{project}",
                index=index,
                generator=prefixed_generator,
                limit=None,
                elastic_url=DEFAULT_ELASTIC,
                output_path=None,
            )

    # when making changes to Elasticsearch
    except OpenSearchException as e:
        _handle_error(
            output,
            f"An ElasticSearch Exception occurred: {str(e)}\n{traceback.format_exc()}",
            OpenSearchException,
        )
    except Exception as e:
        _handle_error(
            output,
            f"An Exception Occurred: {str(e)}\n{traceback.format_exc()}",
            Exception,
        )

    output["logs"].append(f"Loaded {project_id}")
    return True


def _empty_project(
    hostname: str, output: Dict[str, Any], program: str, project: str
) -> None:
    """Clear out graph and flat metadata for project."""
    project_id = f"{program}-{project}"
    try:
        grip_delete(
            hostname,
            graph_name=_get_env_var("GRIP_GRAPH_NAME"),
            project_id=project_id,
            output=output,
            access_token=_get_env_var("ACCESS_TOKEN"),
        )
        output["logs"].append(f"EMPTIED graph for {project_id}")
        for index in INDEX_NAMES:
            meta_flat_delete(project_id=project_id, index=index)
        output["logs"].append(f"EMPTIED flat for {project_id}")
    except Exception as e:
        _handle_error(
            output,
            f"An Exception Occurred emptying project {project_id}: {str(e)}\n{traceback.format_exc()}",
            Exception,
        )


def _run_subprocess(
    cmd: List[str], cwd: str, output: Dict[str, Any], error_msg: str
) -> bool:
    """Run subprocess command and handle errors."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, check=True, capture_output=True, text=True
        )
        logging.info(f"Successfully ran command: {' '.join(cmd)}")
        if result.stdout:
            output["logs"].append(f"STDOUT: {result.stdout.strip()}")
        if result.stderr:
            output["logs"].append(f"STDERR: {result.stderr.strip()}")
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
    """
    formats output as json to stdout so it is passed back to the client,
    most importantly to display relevant logs from the job erroring out
    """
    logging.info(f"[out] {json.dumps(output, separators=(',', ':'))}")


def _handle_error(
    output: Dict[str, Any], message: str, exception_type: type = Exception
):
    """A helper function to log errors, update output, and raise an exception."""
    logging.error(message)
    output["logs"].append(message)
    _write_output_to_client(output)
    raise exception_type(message)


def _validate_and_extract_input(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and extract required fields from the input_data dictionary."""
    required_fields = [
        "ghUserName",
        "ghToken",
        "ghCommitHash",
        "ghRepoUrl",
        "bucketName",
        "profile",
    ]

    for field in required_fields:
        if field not in input_data or not input_data[field]:
            raise ValueError(f"input data must contain a `{field}`")

    return input_data


def _put(
    hostname: str,
    input_data: Dict[str, Any],
    output: Dict[str, Any],
    program: str,
    project: str,
    user: Dict[str, Any],
):
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
            error_msg = (
                f"ERROR 401: No permissions to create project {project} on program {program}. "
                "You can view your project-level permissions with g3t ping"
            )
            _handle_error(output, error_msg, Exception)

        validated_data = _validate_and_extract_input(input_data)

        load_path = pathlib.Path(f"/root/repo/{project}")
        load_path.mkdir(parents=True, exist_ok=True)

        success = _download_and_unzip(
            gh_username=validated_data["ghUserName"],
            gh_token=validated_data["ghToken"],
            gh_repo_url=validated_data["ghRepoUrl"],
            gh_commit_hash=validated_data["ghCommitHash"],
            bucket=validated_data["bucketName"],
            profile=validated_data["profile"],
            project_id=f"{program}-{project}",
            output=output,
            dest_dir=load_path,
        )

        repo_name = pathlib.Path(validated_data["ghRepoUrl"]).stem
        target_dir = os.path.join(os.getcwd(), repo_name)
        load_path = pathlib.Path(f"/root/repo/{project}")

        if not _run_subprocess(
            ["forge", "meta", validated_data["profile"]],
            target_dir,
            output,
            f"ERROR RUNNING FORGE META INIT FOR PROJECT {program}-{project}",
        ):
            return False

        for file in [
            f
            for f in os.listdir(os.path.join(target_dir, META_DIR))
            if f.endswith(".ndjson")
        ]:
            meta_dir = os.path.join(META_DIR, file)
            mv_cmd = ["mv", meta_dir, str(load_path)]
            if not _run_subprocess(mv_cmd, target_dir, output, f"ERROR MOVING {file}"):
                return False

        # Nuke the whole ETL job if the config push doesn't work. -- controversial maybe not do this.
        if not _process_config_files(target_dir, output, hostname):
            return False

        load_success = False
        if success:
            found_files = [str(p) for p in load_path.glob("*")]
            output["files"].extend(found_files)
            logging.info(f"Found files: {found_files}")
            load_success = _load_all(
                hostname, program, project, output, load_path, "work"
            )

        if load_success and _is_guppy_admin(output, program, project, user):
            headers = {
                "Authorization": f"bearer {_get_env_var('ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            }
            try:
                response = requests.post(f"{hostname}/guppy/_refresh", headers=headers)
                response.raise_for_status()
                logging.info(f"Guppy Refresh response:  {response}")
                output["logs"].append(f"Guppy Refresh response:  {response}")
            except requests.exceptions.RequestException as err:
                print(f"An unexpected error occurred: {err}")

        if load_path.exists():
            shutil.rmtree(load_path)
            logging.info(f"Cleaned up directory: {load_path}")

        output["logs"].append(f"EMPTIED flat for {program}-{project}")

    except Exception as e:
        _handle_error(output, f"An unexpected error occurred in _put: {e}", Exception)


def main() -> None:
    """Main entry point for FHIR import/export."""
    token = _get_env_var("ACCESS_TOKEN")
    auth = _auth(token)
    logging.info("[out] authorized successfully")

    hostname = f"https://{_get_env_var('GEN3_HOSTNAME')}"
    logging.info(f"[out] HOSTNAME: {hostname}")
    logging.info("[out] retrieving user info...")

    user = _get_user(auth)
    output = {"user": user["email"], "files": [], "logs": []}
    input_data = _parse_input_data()
    _write_output_to_client(input_data)
    program, project = _get_program_project(input_data)

    method = input_data.get("method")
    if not method:
        raise ValueError("input data must contain a `method`")

    if method.lower() == "put":
        _put(hostname, input_data, output, program, project, user)
    elif method.lower() == "delete":
        _empty_project(hostname, output, program, project)
    else:
        _handle_error(output, f"unknown method {method}", ValueError)

    _write_output_to_client(output)


if __name__ == "__main__":
    main()


def _can_read(output: dict, program: str, project: str, user: dict) -> bool:
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

    required_resources = [f"/programs/{program}", f"/programs/{program}/projects"]
    for required_resource in required_resources:
        if required_resource not in user["resources"]:
            output["logs"].append(f"{required_resource} not found in user resources")
            can_read = False
        else:
            output["logs"].append(f"HAS RESOURCE {required_resource}")

    required_services = [f"/programs/{program}/projects/{project}"]
    for required_service in required_services:
        if required_service not in user["authz"]:
            output["logs"].append(f"{required_service} not found in user authz")
            can_read = False
        else:
            if {"method": "read-storage", "service": "*"} not in user["authz"][
                required_service
            ]:
                output["logs"].append(
                    f"read-storage not found in user authz for {required_service}"
                )
                can_read = False
            else:
                output["logs"].append(
                    f"HAS SERVICE read-storage on resource {required_service}"
                )

    return can_read
