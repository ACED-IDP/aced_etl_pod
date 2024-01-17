import os
import logging
import pathlib
import sys
import json
import subprocess
from datetime import datetime

import click
import yaml
import shutil

from gen3.auth import Gen3Auth

from aced_submission.fhir_store import fhir_get, fhir_put
from aced_submission.meta_graph_load import meta_upload
from aced_submission.meta_flat_load import DEFAULT_ELASTIC, denormalize_patient, load_flat
from gen3.file import Gen3File

from gen3_util.config import Config
from gen3_util.meta.uploader import cp

from pathlib import Path

from iceberg_tools.data.simplifier import simplify_directory

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _get_token() -> str:
    """Get ACCESS_TOKEN from environment"""
    # print("[out] retrieving access token...")
    return os.environ.get('ACCESS_TOKEN', None)


def _auth(access_token: str) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN"""
    # print("[out] authorizing...")
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


def _get_program_project(input_data) -> tuple:
    """Get program and project from input_data"""
    assert 'project_id' in input_data, "project_id not found in INPUT_DATA"
    assert '-' in input_data['project_id'], 'project_id must be in the format <program>-<project>'
    return input_data['project_id'].split('-')


def _get_object_id(input_data) -> str:
    """Get object_id from input_data"""
    return input_data.get('object_id', None)


def _can_create(output, program, user) -> bool:
    """Check if user can create a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        user: user dict from arborist (aka profile)
    """

    can_create = True

    if f"/programs/{program}" not in user['resources']:
        output['logs'].append(f"/programs/{program} not found in user resources")
        can_create = False

    required_resources = [
        f"/programs/{program}/projects"
    ]
    for required_resource in required_resources:
        if required_resource not in user['resources']:
            output['logs'].append(f"{required_resource} not found in user resources")
            can_create = False
        else:
            output['logs'].append(f"HAS RESOURCE {required_resource}")

    required_services = [
        f"/programs/{program}/projects"
    ]
    for required_service in required_services:
        if required_service not in user['authz']:
            output['logs'].append(f"{required_service} not found in user authz")
            can_create = False

    return can_create


def _can_read(output, program, project, user) -> bool:
    """Check if user can read a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        user: user dict from arborist (aka profile)
    """

    can_read = True

    if f"/programs/{program}" not in user['resources']:
        output['logs'].append(f"/programs/{program} not found in user resources")
        can_read = False

    required_resources = [
        f"/programs/{program}/projects/{project}"
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


def _download_and_unzip(object_id, file_path, output, file_name) -> bool:
    """Download and unzip object_id to downloads/{file_path}"""
    try:
        token = _get_token()
        auth = _auth(token)
        file_client = Gen3File(auth)
        full_download_path = (pathlib.Path('downloads') / file_name)
        full_download_path_parent = full_download_path.parent
        full_download_path_parent.mkdir(parents=True, exist_ok=True)
        file_client.download_single(object_id, 'downloads' )
    except Exception as e:
        output['logs'].append(f"An Exception Occurred: {str(e)}")
        output['logs'].append(f"ERROR DOWNLOADING {object_id} {file_path}")
        raise e
        return False

    output['logs'].append(f"DOWNLOADED {object_id} {file_path}")

    cmd = f"unzip -o -j {full_download_path} -d {file_path}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR UNZIPPING /tmp/{object_id}")
        if result.stderr:
            output['logs'].append(result.stderr.read().decode())
        if result.stdout:
            output['logs'].append(result.stdout.read().decode())
        return False

    output['logs'].append(f"UNZIPPED {file_path}")
    return True


def _load_all(study, project_id, output) -> bool:
    config = "/root/config.yaml"
    if not os.path.isfile(config):
        output['logs'].append("config file does not exist")
        return False

    if study is None or study == "":
        output['logs'].append("Please provide a study name")
        return False

    if project_id is None or project_id == "":
        output['logs'].append("Please provide a project_id (program-project)")
        return False

    schema = 'https://aced-public.s3.us-west-2.amazonaws.com/aced.json'

    logs = None

    try:
        program, project = project_id.split('-')
        assert program, output['logs'].append("program is required")
        assert project, output['logs'].append("project is required")

        research_study = f'studies/{study}/extractions/ResearchStudy.ndjson'
        if not os.path.isfile(research_study):
            output['logs'].append("Study not Simplified. Simplifying Study...")
            simplify_directory(f'studies/{study}', pattern="**/*.*",
                               output_path=f'studies/{study}/extractions',
                               schema_path=schema, dialect='PFB',
                               config_path='config.yaml')

        meta_upload(source_path=f'studies/{study}/extractions/',
                    program=program, project=project,
                    credentials_file="/root/.gen3/credentials.json",
                    silent=False, dictionary_path=schema, config_path=config)

        patient_path = f'studies/{study}/extractions/Patient.ndjson'

        if os.path.isfile(patient_path):
            denormalize_patient(patient_path)
            load_flat(project_id=project_id, index="patient",
                      path=patient_path, limit=None,
                      elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)
        else:
            load_flat(project_id=project_id, index="patient",
                      path='/dev/null', limit=None,
                      elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)

        observation_path = f'studies/{study}/extractions/Observation.ndjson'
        if os.path.isfile(observation_path):
            load_flat(project_id=project_id, index="observation",
                      path=observation_path, limit=None,
                      elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)
        else:
            load_flat(project_id=project_id, index="observation",
                      path='/dev/null', limit=None,
                      elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)

        file_path = f'studies/{study}/extractions/DocumentReference.ndjson'
        if os.path.isfile(file_path):
            load_flat(project_id=project_id, index="file", path=file_path,
                      limit=None, elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)
        else:
            load_flat(project_id=project_id, index="file", path='/dev/null',
                      limit=None, elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)

        logs = fhir_put(project_id, path=f'studies/{study}',
                        elastic_url=DEFAULT_ELASTIC)
        yaml.dump(logs, sys.stdout, default_flow_style=False)

    except Exception as e:
        output['logs'].append(f"An Exception Occurred: {str(e)}")
        output['logs'].append(f"LOADED {study}")
        if logs is not None:
            output['logs'].extend(logs)
        return False

    output['logs'].append(f"LOADED {study}")
    if logs is not None:
        output['logs'].extend(logs)
    return True


def _get(input_data, output, program, project, user) -> str:
    """Export data from the fhir store to bucket, returns object_id."""
    can_read = _can_read(output, program, project, user)
    if not can_read:
        output['logs'].append(f"No read permissions on {program}-{project}")
        return None

    study_path = f"studies/{project}"
    project_id = f"{program}-{project}"

    logs = fhir_get(f"{program}-{project}", study_path, DEFAULT_ELASTIC)
    output['logs'].extend(logs)

    # zip and upload the exported files to bucket
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    object_name = f'{project_id}_{now}_SNAPSHOT.zip'

    config = Config()
    cp_result = cp(
        config=config,
        from_=study_path,
        project_id=project_id,
        metadata={'submitter': None, 'metadata_version': '0.0.1', 'is_metadata': True, 'is_snapshot': True},
        user=user,
        object_name=object_name,
        ignore_state=False)

    output['logs'].append(cp_result['msg'])
    object_id = cp_result['object_id']

    return object_id


def main():
    token = _get_token()
    auth = _auth(token)

    # print("[out] authorized successfully")

    # print("[out] retrieving user info...")
    user = _user(auth)

    output = {'user': None, 'files': [], 'logs': []}
    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller
    # print(f"[out] {json.dumps(user, separators=(',', ':'))}")

    # output['env'] = {k: v for k, v in os.environ.items()}

    input_data = _input_data()
    print(f"[out] {json.dumps(input_data, separators=(',', ':'))}")

    program, project = _get_program_project(input_data)

    method = input_data.get("method", None)
    assert method, "input data must contain a `method`"
    if method.lower() == 'put':
        # read from bucket, write to fhir store
        _put(input_data, output, program, project, user)
        # after pushing commits, create a snapshot file
        object_id = _get(input_data, output, program, project, user)
        output['snapshot'] = {'object_id': object_id}
    elif method.lower() == 'get':
        # read fhir store, write to bucket
        object_id = _get(input_data, output, program, project, user)
        output['object_id'] = object_id
    else:
        raise Exception(f"unknown method {method}")

    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller
    print(f"[out] {json.dumps(output, separators=(',', ':'))}")


def _put(input_data, output, program, project, user):
    """Import data from bucket to graph, flat and fhir store."""
    # check permissions
    can_create = _can_create(output, program, user)
    output['logs'].append(f"CAN CREATE: {can_create}")
    assert can_create, f"No create permissions on {program}"
    assert 'push' in input_data, "input data must contain a `push`"
    for commit in input_data['push']['commits']:
        assert 'object_id' in commit, "commit must contain an `object_id`"
        object_id = commit['object_id']
        assert object_id, "object_id must not be empty"
        assert 'commit_id' in commit, "commit must contain a `commit_id`"
        commit_id = commit['commit_id']
        assert commit_id, "commit_id must not be empty"
        file_path = f"/root/studies/{project}/commits/{commit_id}"
        pathlib.Path(file_path).mkdir(parents=True, exist_ok=True)
        # get the meta data file
        if _download_and_unzip(object_id, file_path, output, commit['meta_path']):

            # tell user what files were found
            for _ in pathlib.Path(file_path).glob('*'):
                output['files'].append(str(_))

            # load the study into the database and elastic search
            _load_all(project, f"{program}-{project}", output)

        shutil.rmtree(f"/root/studies/{project}")


if __name__ == '__main__':
    main()
