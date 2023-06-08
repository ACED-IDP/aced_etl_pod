#!/bin/bash
# Script to setup environment for the ACED ETL process

# Install network and development tools. gcc is necessary for installing the gen3 SDK.
apt-get -y update
apt-get -y --no-install-recommends install  \
           build-essential \
           curl \
           gcc \
           git \
           iputils-ping \
           jq \
           libmagic-dev \
           libpq-dev \
           postgresql-client \
           python3-dev \
           unzip \
           vim

rm -rf /var/lib/apt/lists/* \

cd ~
python3 -m venv venv
source venv/bin/activate

pip install git+https://github.com/ACED-IDP/aced_etl_pod

# mac silicon, build from scratch, avoids Postgresql SCRAM authentication problem
pip install --no-cache-dir psycopg2

curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
./aws/install
rm awscliv2.zip