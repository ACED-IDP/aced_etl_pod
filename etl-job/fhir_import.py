import os
import logging
import sys
import json
import subprocess

from gen3.auth import Gen3Auth

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _get_token() -> str:
    """Get ACCESS_TOKEN from environment"""
    # print("[out] retrieving access token...")
    return os.environ['ACCESS_TOKEN']


def _auth(access_token: str) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN"""
    # print("[out] authorizing...")
    return Gen3Auth(refresh_file=f"accesstoken:///{access_token}")


def _user(auth: Gen3Auth) -> dict:
    """Get user info"""
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
    """Check if user can create a project in the given program."""
    assert f"/programs/{program}" in user['resources'], f"/programs/{program} not found in user resources"
    can_create = True
    required_resources = [
        '/services/sheepdog/submission/program',
        '/services/sheepdog/submission/project',
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
        else:
            if {'method': '*', 'service': 'sheepdog'} not in user['authz'][required_service]:
                output['logs'].append(f"sheepdog not found in user authz for {required_service}")
                can_create = False
            else:
                output['logs'].append(f"HAS SERVICE sheepdog on resource {required_service}")

    return can_create


def _download_and_unzip(object_id, file_path, output) -> bool:
    """Download and unzip object_id to file_path"""
    cmd = f"gen3 file download-single {object_id} --path /tmp/{object_id}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR DOWNLOADING {object_id} /tmp/{object_id}")
        output['logs'].append(result.stderr.read().decode())
        output['logs'].append(result.stdout.read().decode())
        return False
    output['logs'].append(f"DOWNLOADED {object_id} {file_path}")
    cmd = f"unzip /tmp/{object_id}-d {file_path}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR UNZIPPING /tmp/{object_id}")
        output['logs'].append(result.stderr.read().decode())
        output['logs'].append(result.stdout.read().decode())
        return False

    output['logs'].append(f"UNZIPPED {file_path}")
    return True


def _load_all(study, project_id, output) -> bool:
    """Use script to load study."""
    cmd = f"load_all {study} {project_id}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR LOADING {study}")
        output['logs'].append(result.stderr.read().decode())
        output['logs'].append(result.stdout.read().decode())
        return False

    output['logs'].append(f"LOADED {study}")
    return True


def _main():
    """Main function"""

    auth = _auth(_get_token())
    # print("[out] authorized successfully")

    # print("[out] retrieving user info...")
    user = _user(auth)

    output = {'user': user, 'files': [], 'logs': []}

    # for _ in pathlib.Path('.').glob('*.*'):
    #     if _.is_file():
    #         output['files'].append(str(_))

    # output['env'] = {k: v for k, v in os.environ.items()}

    input_data = _input_data()
    program, project = _get_program_project(input_data)

    # check permissions
    can_create = _can_create(output, program, user)
    output['logs'].append(f"CAN CREATE: {can_create}")

    file_path = f"/root/studies/{project}/"
    if can_create:
        object_id = _get_object_id(input_data)
        if object_id:
            if _download_and_unzip(object_id, file_path, output):
                _load_all(project, f"{program}-{project}", output)
        else:
            output['logs'].append(f"OBJECT ID NOT FOUND")

    # note, only the last output is returned to the caller
    print(f"[out] {json.dumps(output, separators=(',', ':'))}")


if __name__ == '__main__':
    _main()
