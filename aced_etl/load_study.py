import pathlib
import subprocess

import click
import logging

from gen3_util.config import ensure_auth

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


def run_cmd(command_line) -> str:
    """Run a command line, return stdout."""
    try:
        logger.debug(command_line)
        return subprocess.check_output(command_line, shell=True).decode("utf-8").rstrip()
    except Exception as exc:
        logger.error(exc)
        raise exc


@click.command()
@click.argument('project_id')
@click.argument('bucket_name')
@click.option('--skip-upload/--no-skip-upload', is_flag=True, default=False, show_default=True,
              help="Skip uploading files to the bucket.")
@click.option('--skip-graph/--no-skip-graph', is_flag=True, default=False, show_default=True, help="Skip loading the graph.")
@click.option('--skip-flat/--no-skip-flat', is_flag=True, default=False, show_default=True,
              help="Skip loading the flat files into elastic.")
def _load_study(project_id: str, bucket_name: str, skip_upload: bool, skip_graph: bool, skip_flat: bool):
    """Load a study into the data commons.

    PROJECT_ID is the name of the program-project to load.
    BUCKET_NAME is the name of the bucket to store data.
    """
    load_study(project_id, bucket_name, skip_upload, skip_graph, skip_flat)


def load_study(project_id: str, bucket_name: str, skip_upload=False, skip_graph=False, skip_flat=False):
    """Load a study into the data commons.

    \b
    PROJECT_ID is the name of the program-project to load.
    BUCKET_NAME is the name of the bucket to store data.
    """
    program, project = project_id.split('-')
    assert pathlib.Path(f"studies/{project}").is_dir(), f"studies/{project} does not exist"

    if not skip_upload:
        cmd = f"aced_submission files upload --program {program} --project {project} --bucket_name {bucket_name}  --document_reference_path studies/{project}  --duplicate_check"
        print(run_cmd(cmd))

    if not skip_graph:
        cmd = f"aced_submission meta graph upload --source_path studies/{project}/extractions/ --program {program} --project $study  --dictionary_path https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json"
        print(run_cmd(cmd))

    if not skip_flat:
        cmd = f"aced_submission meta flat denormalize-patient --input_path studies/{project}/extractions/Patient.ndjson"
        print(run_cmd(cmd))

        cmd = f"aced_submission meta flat load --project_id {program}-{project} --index patient --path studies/{project}/extractions/Patient.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json"
        print(run_cmd(cmd))

        cmd = f"aced_submission meta flat load --project_id {program}-{project} --index file --path studies/{project}/extractions/DocumentReference.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json"
        print(run_cmd(cmd))

        cmd = f"aced_submission meta flat load --project_id {program}-{project} --index observation --path studies/{project}/extractions/Observation.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json"
        print(run_cmd(cmd))


# TODO: read from config
STUDIES = """Alcoholism aced-development-ohsu-data-bucket
Alzheimers aced-development-ucl-data-bucket
Breast_Cancer aced-development-manchester-data-bucket
Diabetes aced-development-ucl-data-bucket
Lung_Cancer aced-development-manchester-data-bucket
Prostate_Cancer aced-development-stanford-data-bucket
Colon_Cancer aced-development-stanford-data-bucket
""".split("\n")


@click.command()
@click.option('--skip-upload/--no-skip-upload', is_flag=True, default=False, show_default=True,
              help="Skip uploading files to the bucket.")
@click.option('--skip-graph/--no-skip-graph', is_flag=True, default=False, show_default=True,
              help="Skip loading the graph.")
@click.option('--skip-flat/--no-skip-flat', is_flag=True, default=False, show_default=True,
              help="Skip loading the flat files into elastic.")
def load_studies(skip_upload: bool, skip_graph: bool, skip_flat: bool):
    """Load all studies into the data commons."""
    if click.confirm('Load all studies. Do you want to continue?'):
        for study_bucket in STUDIES:
            study, bucket = study_bucket.split(' ')
            load_study(project_id=f"aced-{study}", bucket_name=bucket, skip_upload=skip_upload, skip_graph=skip_graph)
