# Building the ETL Docker Image

The job is a native Go executable. It keeps the existing Sower environment
contract (`INPUT_DATA`, `ACCESS_TOKEN`, and `GEN3_HOSTNAME`) and uses the
exported Git-DRS and Forge Go command packages directly. A successful `put`
clones and hydrates the repository, generates metadata through Forge's Go
package, uploads the complete FHIR snapshot through Loom's multipart generation
API. Without a repository Explorer definition it validates/materializes the
default recipe and activates the generation as before. With
`CONFIG/<project>.json`, it posts the exact authored `ExplorerConfigV2` JSON to
Loom's repository Explorer REST endpoint. The document may be a recipe-only
schema/readiness baseline or a complete packet that also contains ETL-authored
views, filters, charts, shared filters, and file actions. Loom validates,
materializes, preserves, and exposes a complete packet as the repository
default's `activeConfig`; the default still has no editable draft. Gecko is not
part of this deployment path.

## 1. Authenticate with quay.io
```sh
docker login quay.io
# Login Succeeded
```

## 2. Build the image
```sh
docker build -t quay.io/ohsu-comp-bio/aced-etl-job . --platform linux/amd64
```

## 3. Push to the quay.io repository
```sh
docker push quay.io/ohsu-comp-bio/aced-etl-job
```


## 4. Multi platform builds

```sh
# see https://docs.docker.com/build/building/multi-platform/
# do this once
docker buildx create --name aced-builder --bootstrap --use

# build and push ( this took about 20 min ! )
docker buildx build --platform linux/amd64,linux/arm64 -t quay.io/ohsu-comp-bio/aced-etl:latest --push .

# confirm it worked
docker buildx imagetools inspect quay.io/ohsu-comp-bio/aced-etl:testing | grep Platform
  Platform:    linux/amd64
  Platform:    linux/arm64
  Platform:    unknown/unknown
  Platform:    unknown/unknown
```

View on quay.io

<img width="1134" alt="image" src="https://github.com/ACED-IDP/data_model/assets/47808/5102dbae-71e2-473f-95de-7f43840c034e">


## 5. Testing/Developing a job from the pod 


```commandline
# set expected variables
export INPUT_DATA='{"object_id": "7289c112-57d0-5916-864a-f516d4e6c901", "project_id": "test-myproject", "method": "put" }'
export schema=https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
export project_id=test-myproject
export study=myproject

# ensure that the credentials are available in the pod, the job will read ACCESS_KEY if its there, otherwise defaults to 
ls -1 ~/.gen3/credentials.json

# run the same job locally
./calypr-etl

# Optional Loom override for the database-only migration. If omitted, the job
# uses https://$GEN3_HOSTNAME/loom.
export LOOM_URL=https://loom.example.org

# Optional recipe override. The default is calypr-meta-default.
export LOOM_RECIPE_NAME=calypr-meta-default

# Optional deployment assertion. Loom's registered recipe reports the actual
# translation version; setting this catches Helm/ETL drift.
# export LOOM_TRANSLATION_VERSION=gen3-util-development-d461f11c7f0ccb078128349ccffd377e4014b62a-extension-columns-v2-research-subject
# This is used only for projects without a repository Explorer definition.
export LOOM_RECIPE_OUTPUTS=DocumentReference,ResearchSubject,MedicationAdministration,Specimen,GroupMember

# Alternatively provide exact selectors as JSON (useful for a non-default recipe).
# export LOOM_REQUIRED_DATAFRAME_SELECTORS='[{"recipe":"my-recipe","output":"DocumentReference"}]'
```

The ETL uses the Git commit hash as the immutable Loom generation key. Re-running
the same commit is safe: generation loading and recipe publication are keyed by
that commit. To rebuild Loom-derived state after a profiler or ingestion upgrade
without creating a source repository commit, add `"forceLoomRefresh": true` to
`INPUT_DATA`. The job still checks out `ghCommitHash`, but creates one fresh,
immutable Loom generation for that run and uses it consistently through upload,
semantic profiling, dataframe materialization, Explorer deployment, and
activation. The ETL uses Loom's existing multipart
`POST /api/v1/datasets/:project/generations/:generation` contract and the
synchronous `materializeDataframeRecipeBundle` GraphQL mutation; it does not
invent per-resource upload or finalize endpoints. Generation upload uses
`defer_activation=true`. A repository definition must be the exact
`ExplorerConfigV2` shape with `apiVersion:
"loom.calypr.org/explorer-config/v2"`, its canonical project ID, a repository
`default` Explorer, and an executable `recipe`. The repository default is a
schema/readiness baseline and may also carry a complete presentation layer;
when present, Loom exposes that packet as `default.activeConfig`. Legacy `explorerConfig`,
`tabs`, and V1 opaque-selection JSON also fail before activation.
ETL sends the document unchanged to
`POST /api/v1/projects/:project/generations/:generation/explorer-config`, with
the checked-out commit in `X-Loom-Source-Commit`. A successful response is the
atomic activation; ETL does not call the old Explorer GraphQL mutation or a
second activation endpoint. Before reporting success, ETL verifies that Loom
returned an activated execution with the exact project, generation, rewritten
repository recipe name, and commit-derived translation version. This prevents
a deployment from succeeding while exposing selector metadata that cannot
address the materialized dataframe.
