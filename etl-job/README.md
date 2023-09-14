# Building the ETL Docker Image

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
docker buildx build --platform linux/amd64,linux/arm64 -t quay.io/ohsu-comp-bio/aced-etl-job:testing --push .

# confirm it worked
docker buildx imagetools inspect quay.io/ohsu-comp-bio/aced-etl-job:testing | grep Platform
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

# whatever you want to test
# ./load_all 
# or
# python3 fhir_import.py
```