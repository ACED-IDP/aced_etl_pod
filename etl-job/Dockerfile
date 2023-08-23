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
           automake \
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
RUN source venv/bin/activate && pip install wheel
RUN source venv/bin/activate && pip install 'git+https://github.com/ACED-IDP/aced_etl_pod' && pip install --no-cache-dir psycopg2
RUN source venv/bin/activate && pip install yq

# TODO - should this belong in etl pod repo / helm?
RUN curl  https://raw.githubusercontent.com/bmeg/iceberg-schema-tools/main/config.yaml -o config.yaml

RUN mkdir ~/.aws
RUN mkdir ~/.gen3

COPY . /root
ENV PYTHONUNBUFFERED=1
RUN chmod +x /root/entrypoint.sh
RUN chmod +x load_all

RUN mkdir /root/studies
RUN ls -la /root

ENTRYPOINT [ "/root/entrypoint.sh" ]
CMD [ "python", "fhir_import.py" ]
