
#!/bin/bash
set -e
# Any subsequent(*) commands which fail will cause the shell script to exit immediately




# now load it all up

echo "Load all studies"

#  aced_submission files upload --program aced --project $study --bucket_name `eval "echo \\$${study}_BUCKET"`  --document_reference_path studies/$study  --duplicate_check

for study in ${studies[@]}; do
  aced_submission meta graph upload --source_path studies/$study/extractions/ --program aced --project $study  --dictionary_path https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
  aced_submission meta flat denormalize-patient --input_path studies/$study/extractions/Patient.ndjson
  aced_submission meta flat load --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
  aced_submission meta flat load --project_id aced-$study --index file --path /studies/$study/extractions/DocumentReference.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
  aced_submission meta flat load --project_id aced-$study --index observation --path /studies/$study/extractions/Observation.ndjson --schema_path  https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json


done



