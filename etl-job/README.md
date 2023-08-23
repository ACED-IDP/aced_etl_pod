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
