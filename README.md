# etl
etl worker pod

## use case

> As an ACED devops engineer, in order to maintain the ACED datasets,I need to be able to run the ETL process on a regular basis, leveraging the environment provided to the etl k8s pod.


## design

![image](./docs/aced-etl.svg)

## implementation

### Docker k8s pod image

See [docker/etl-docker.md](./docker/etl-docker.md)

### Assumptions:ETL image file structure

* All environments (local, staging, production):

  * The root home directory will have virtual environment with all dependencies loaded
    * aced_submission
    * gen3_util
    * iceberg
    * aws cli
    * jq, vi, curl, psql, etc. 
  
  * The Helm chart will mount the following directories into the ETL pod: 
    * `/creds` - contains all credential files required for the ETL process.
    * environmental variables
    * TODO how will the Helm chart configure ~/.aws directory
 
    The `/creds` directory contains all credential files required for the ETL process.

        ```
        /creds
        ├── credentials.json
        ├── sheepdog-creds
        │   ├── database -> ..data/database
        │   ├── dbcreated -> ..data/dbcreated
        │   ├── host -> ..data/host
        │   ├── password -> ..data/password
        │   ├── port -> ..data/port
        │   └── username -> ..data/username
        └── user.yaml
        ```
    The `~/.aws` directory will have:
  
   ```
        ~/.aws
        ├── config
        └── credentials
  
        # TODO: How will the Helm chart configure ~/.aws directory?
        cat ~/.aws/credentials
        [fencebot] 
        aws_access_key_id = YYYYY
        aws_secret_access_key = YYYYY
        [etlbot] 
        aws_access_key_id = YYYYY
        aws_secret_access_key = YYYYY

    ```
  
  * TODO: The Helm chart will make the `fence.ALLOWED_DATA_UPLOAD_BUCKETS` available Where? How?

* Staging/Production:
  * The ETL user will download ~/studies and ~/output from the S3 bucket.

* Local:
  * The ETL user can mount the local directories into the ETL pod at ~/studies and ~/output

          * `~/studies` - meta data files for the studies to be loaded into the Gen3 endpoint.
          * `~/output` - files to be loaded into indexd

## use

### Testing fence's ability to sign URLs

```sh
 AWS_PROFILE=fencebot python3 ./put_object aced-development-data-bucket test2.txt put
```

### Initializing programs and projects in the Gen3 endpoint 

```sh
TODO
```


### Loading meta data into the Gen3 endpoint

```sh
TODO
```

### Loading files into indexd

```sh
TODO
```

### Truncating sheepdog data

```sh
TODO
```
