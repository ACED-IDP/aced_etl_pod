# set base image (host OS)
FROM python:3.9.16-slim-buster

# install dependencies
RUN apt-get -y update
RUN apt-get -y --no-install-recommends install  \
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
           vim \
           autoconf \
           libtool

RUN rm -rf /var/lib/apt/lists/*

# set the working directory in the container

WORKDIR /root

RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
RUN unzip awscliv2.zip
RUN ./aws/install
RUN rm awscliv2.zip

SHELL ["/bin/bash", "-c"]
RUN python3 -m venv venv
RUN source venv/bin/activate && pip install 'git+https://github.com/ACED-IDP/aced_etl_pod' && pip install --no-cache-dir psycopg2
RUN source venv/bin/activate && pip install yq
RUN source venv/bin/activate && pip install wheel


