
#!/bin/bash
set -e
# Any subsequent(*) commands which fail will cause the shell script to exit immediately

# gather the configured buckets
gen3_util buckets ls > /tmp/buckets.yaml

# assign them to projects
export Alcoholism_BUCKET=aced-development-ohsu-data-bucket
export Alzheimers_BUCKET=aced-development-ucl-data-bucket
export Breast_Cancer_BUCKET=aced-development-manchester-data-bucket
export Colon_Cancer_BUCKET=aced-development-stanford-data-bucket
export Diabetes_BUCKET=aced-development-ucl-data-bucket
export Lung_Cancer_BUCKET=aced-development-manchester-data-bucket
export Prostate_Cancer_BUCKET=aced-development-stanford-data-bucket
export NVIDIA_BUCKET=aced-development-ohsu-data-bucket



export studies=(Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer)
export buckets=($Alcoholism_BUCKET $Alzheimers_BUCKET $Breast_Cancer_BUCKET $Colon_Cancer_BUCKET $Diabetes_BUCKET $Lung_Cancer_BUCKET $Prostate_Cancer_BUCKET)

# check to ensure all buckets exist
echo "check to ensure all buckets exist"
for bucket in ${buckets[@]}; do
  grep $bucket /tmp/buckets.yaml
done

# create all studies
# check to ensure all studies exist
gen3_util projects touch --all > /dev/null

gen3_util --format json projects ls > /tmp/projects.json
cat /tmp/projects.json | jq -rc '.projects | to_entries[] | [.key, .value.exists] | @tsv' | grep true > /tmp/existing_projects.txt

echo "check to make sure program and projects exist"
for study in ${studies[@]}; do
  grep $study /tmp/existing_projects.txt
done

