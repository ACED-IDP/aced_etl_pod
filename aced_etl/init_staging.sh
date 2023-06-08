
#!/bin/bash
set -e
# Any subsequent(*) commands which fail will cause the shell script to exit immediately

# gather the configured buckets
gen3_util buckets ls > /tmp/buckets.yaml

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

