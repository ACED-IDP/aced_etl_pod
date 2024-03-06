import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import traceback
from datetime import datetime

import yaml
from aced_submission.fhir_store import fhir_get, fhir_put, fhir_delete
from aced_submission.meta_flat_load import DEFAULT_ELASTIC, \
    denormalize_patient, load_flat, delete_not_in_manifest
from aced_submission.meta_flat_load import delete as meta_flat_delete
from aced_submission.meta_graph_load import meta_upload, empty_project
from aced_submission.meta_query import counts_project_index
from aced_submission.meta_discovery_load import discovery_load, \
    discovery_delete, discovery_get
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ElasticsearchException
from gen3.auth import Gen3Auth
from gen3.file import Gen3File
from gen3_util.config import Config
from gen3_util.meta.uploader import cp
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


def _get_program_project(input_data: dict) -> tuple:
    """Get program and project from input_data"""
    assert 'project_id' in input_data, "project_id not found in INPUT_DATA"
    assert '-' in input_data['project_id'], 'project_id must be in the format <program>-<project>'
    return input_data['project_id'].split('-')


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


def _download(object_id: str,
              output: dict,
              file_name: str) -> tuple[bool, any]:
    '""downloads a file from a bucket'
    try:
        token = _get_token()
        auth = _auth(token)
        file_client = Gen3File(auth)
        full_download_path = (pathlib.Path('downloads') / file_name)
        full_download_path_parent = full_download_path.parent
        full_download_path_parent.mkdir(parents=True, exist_ok=True)
        file_client.download_single(object_id, 'downloads')
    except Exception as e:
        output['logs'].append(f"An Exception Occurred: {str(e)}")
        output['logs'].append(f"ERROR DOWNLOADING {object_id} \
TO PATH: {full_download_path}")
        return False, output, full_download_path

    output['logs'].append(f"DOWNLOADED {object_id}")
    return True, output, full_download_path


def _download_and_unzip(object_id: str,
                        file_path: str,
                        output: dict,
                        file_name: str) -> tuple[bool, any]:
    """Download and unzip object_id to downloads/{file_path}"""
    success, output, full_download_path = _download(object_id,
                                                    output, file_name)
    if success:
        cmd = f"unzip -o -j {full_download_path} -d {file_path}".split()
        result = subprocess.run(cmd)
        if result.returncode != 0:
            output['logs'].append(f"ERROR UNZIPPING /tmp/{object_id}")
            if result.stderr:
                output['logs'].append(result.stderr.read().decode())
            if result.stdout:
                output['logs'].append(result.stdout.read().decode())
            return False, output

        output['logs'].append(f"UNZIPPED {file_path}")
        return True, output
    return False, output


def _load_all(study: str,
              project_id: str,
              output: dict,
              file_path: str,
              schema: str) -> bool:
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

    logs = None

    try:
        program, project = project_id.split('-')
        assert program, output['logs'].append("program is required")
        assert project, output['logs'].append("project is required")

        file_path = pathlib.Path(file_path)
        extraction_path = file_path / 'extractions'
        research_study = str(extraction_path / 'ResearchStudy.ndjson')
        patient_path = str(extraction_path / 'Patient.ndjson')
        observation_path = str(extraction_path / 'Observation.ndjson')
        document_reference_path = str(extraction_path / 'DocumentReference.ndjson')

        file_path = str(file_path)
        extraction_path = str(extraction_path)

        output['logs'].append(f"Simplifying study: {file_path}")
        simplify_directory(file_path, pattern="**/*.*",
                           output_path=extraction_path,
                           schema_path=schema, dialect='PFB',
                           config_path='config.yaml')

        meta_upload(source_path=extraction_path,
                    program=program, project=project,
                    silent=False, dictionary_path=schema, config_path=config)

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

        if os.path.isfile(document_reference_path):
            load_flat(project_id=project_id, index="file",
                      path=document_reference_path,
                      limit=None, elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)
        else:
            load_flat(project_id=project_id, index="file", path='/dev/null',
                      limit=None, elastic_url=DEFAULT_ELASTIC,
                      schema_path=schema, output_path=None)

        # Load disovery page if research study exists in commit
        if os.path.isfile(research_study):
            output['logs'].append("Writing to metadata-service")
            elastic = Elasticsearch([DEFAULT_ELASTIC], request_timeout=120)
            _patients_count = counts_project_index(elastic=elastic, project_id=project_id, index="gen3.aced.io_patient_0")
            with open(research_study, "r") as study:
                """
                Is there ever a scenario where the researchStudy will have more than one line?

                Example autogenerated research study from g3t:

                {'id': 'a45ea123-aeda-5982-aeac-79bfb1bf5920', 'name': 'research_study', 'relations': [],
                'object': {'id': 'a45ea123-aeda-5982-aeac-79bfb1bf5920',
                'status': 'active', 'description': 'Skeleton ResearchStudy for synthea-delete',
                'resourceType': 'ResearchStudy', 'identifier': ['synthea_delete#synthea-delete'],
                'identifier_coding': ['https://aced-idp.org/synthea-delete#synthea-delete']}}
                """
                study_meta = json.loads(study.readline())
                discovery_load(project_id, _patients_count, study_meta["object"]["description"], study_meta["object"]["identifier_coding"], overwrite=True)

        logs = fhir_put(project_id, path=file_path,
                        elastic_url=DEFAULT_ELASTIC)
        yaml.dump(logs, sys.stdout, default_flow_style=False)

    except ElasticsearchException as e:
        output['logs'].append(f"An ElasticSearch Exception occurred: {str(e)}")
        tb = traceback.format_exc()
        if logs is not None:
            output['logs'].extend(logs)
            output['logs'].append(tb)
        return False

    except Exception as e:
        output['logs'].append(f"An Exception Occurred: {str(e)}")
        tb = traceback.format_exc()
        if logs is not None:
            output['logs'].extend(logs)
            output['logs'].append(tb)
        return False

    output['logs'].append(f"LOADED {study}")
    if logs is not None:
        output['logs'].extend(logs)
    return True


def _get(output: dict,
         program: str,
         project: str,
         user: dict) -> str:
    """Export data from the fhir store to bucket, returns object_id."""
    can_read = _can_read(output, program, project, user)
    if not can_read:
        output['logs'].append(f"No read permissions on {program}-{project}")
        return None

    study_path = f"studies/{project}"
    project_id = f"{program}-{project}"

    # ensure we wait for the index to be refreshed before we query it
    elastic = Elasticsearch([DEFAULT_ELASTIC], request_timeout=120)
    elastic.indices.refresh(index='fhir')

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
        metadata={'submitter': None, 'metadata_version': '0.0.1',
                  'is_metadata': True, 'is_snapshot': True},
        user=user,
        object_name=object_name,
        ignore_state=False)

    output['logs'].append(cp_result['msg'])
    object_id = cp_result['object_id']

    return object_id


def _reset_to_commit_id(output: dict,
                        program: str,
                        project: str,
                        user: dict,
                        commit_id: str,
                        object_id: str,
                        config_path: str,
                        dictionary_path: str = None,):
    """Retrieve uploaded metadata manifest and reset elasticsearch
       Indices to the manifest state"""

    try:
        can_create = _can_create(output, program, project, user)
        assert can_create, f"No create permissions on {program}"

        output["logs"].append(f"reseting to {commit_id}")
        file_path = f"/root/studies/{project}/commits/{commit_id}"
        pathlib.Path(file_path).mkdir(parents=True, exist_ok=True)

        source_path = f".g3t/state/{f'{program}-{project}'}/commits/{commit_id}/meta-index.ndjson"
        success, output, full_download_path = _download(object_id,
                                                        output, source_path)
        if success:
            shutil.move(full_download_path, file_path)
            for _ in pathlib.Path(file_path).glob('*'):
                output['files'].append(str(_))

            manifest_ids = []
            with open(f"{file_path}/meta-index.ndjson", "r") as f:
                for line in f:
                    manifest_ids.append(json.loads(line))

            empty_project(program=program, project=project,
                          dictionary_path=dictionary_path,
                          config_path=config_path, manifest=manifest_ids)

            output = delete_not_in_manifest(manifest_ids, index=None,
                                            project_id=f"{program}-{project}",
                                            output=output)

            output = delete_not_in_manifest(manifest_ids, index="fhir",
                                            project_id=f"{program}-{project}",
                                            output=output)
    except Exception as e:
        output['logs'].append(f"An Exception Occurred emptying project {program}-{project}: {str(e)}")
        tb = traceback.format_exc()
        output['logs'].append(tb)


def _empty_project(output: dict,
                   program: str,
                   project: str,
                   user: dict,
                   dictionary_path: str = None,
                   config_path: str = None):
    """Clear out graph and flat metadata for project """
    # check permissions
    try:
        can_create = _can_create(output, program, project, user)
        assert can_create, f"No create permissions on {program}"

        empty_project(program=program, project=project,
                      dictionary_path=dictionary_path,
                      config_path=config_path,
                      manifest=None)
        output['logs'].append(f"EMPTIED graph for {program}-{project}")

        for index in ["patient", "observation", "file"]:
            meta_flat_delete(project_id=f"{program}-{project}", index=index)
        output['logs'].append(f"EMPTIED flat for {program}-{project}")

        fhir_delete(f"{program}-{project}", DEFAULT_ELASTIC)
        output['logs'].append(f"EMPTIED FHIR STORE for {program}-{project}")

        discovery_data = discovery_get(f"{program}-{project}")
        if discovery_data not in [None, {}]:
            discovery_delete(f"{program}-{project}")

    except Exception as e:
        output['logs'].append(f"An Exception Occurred emptying project {program}-{project}: {str(e)}")
        tb = traceback.format_exc()
        output['logs'].append(tb)


def main():
    token = _get_token()
    auth = _auth(token)

    # print("[out] authorized successfully")

    # print("[out] retrieving user info...")
    user = _user(auth)

    output = {'user': user['email'], 'files': [], 'logs': []}
    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller
    # print(f"[out] {json.dumps(user, separators=(',', ':'))}")

    output['env'] = {k: v for k, v in os.environ.items()}

    input_data = _input_data()
    print(f"[out] {json.dumps(input_data, separators=(',', ':'))}")

    program, project = _get_program_project(input_data)

    schema = os.getenv('DICTIONARY_URL', None)
    if schema is None:
        schema = 'https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json'
        output['logs'].append(
            f"DICTIONARY_URL not found in environment using {schema}")

    method = input_data.get("method", None)
    assert method, "input data must contain a `method`"
    if method.lower() == 'put':
        # read from bucket, write to fhir store
        _put(input_data, output, program, project, user, schema)
        # after pushing commits, create a snapshot file
        object_id = _get(output, program, project, user)
        output['snapshot'] = {'object_id': object_id}
    elif method.lower() == 'get':
        # read fhir store, write to bucket
        object_id = _get(output, program, project, user)
        output['object_id'] = object_id
    elif method.lower() == 'delete':
        commit_id = input_data.get("commit_id", None)
        object_id = input_data.get("object_id", None)
        if commit_id is not None and object_id is not None:
            _reset_to_commit_id(output, program, project,
                                user, commit_id, object_id,
                                config_path="config.yaml",
                                dictionary_path=schema)
        elif commit_id is None:
            _empty_project(output, program, project, user,
                           dictionary_path=schema, config_path="config.yaml")
    else:
        raise Exception(f"unknown method {method}")

    # note, only the last output (a line in stdout with `[out]` prefix)
    # is returned to the caller
    print(f"[out] {json.dumps(output, separators=(',', ':'))}")


def _put(input_data: dict,
         output: dict,
         program: str,
         project: str,
         user: dict,
         schema: str):
    """Import data from bucket to graph, flat and fhir store."""
    # check permissions
    can_create = _can_create(output, program, project, user)
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
        success, output = _download_and_unzip(object_id, file_path,
                                              output, commit['meta_path'])
        if success:
            # tell user what files were found
            for _ in pathlib.Path(file_path).glob('*'):
                output['files'].append(str(_))

            # load the study into the database and elastic search
            _load_all(project, f"{program}-{project}",
                      output, file_path, schema)

        shutil.rmtree(f"/root/studies/{project}")


if __name__ == '__main__':
    main()
