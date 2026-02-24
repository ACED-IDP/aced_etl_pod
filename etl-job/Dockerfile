FROM golang:1.24.2-bookworm AS go-builder

RUN go install github.com/calypr/forge@e537c5c12109aa84bda8c6cc54ea129e1b28a8ce
RUN go install github.com/calypr/git-drs@a0aad46c041f997c80c43c64842168356fccf5eb


FROM python:3.12-bookworm

RUN apt-get -y update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    gcc \
    git \
    git-lfs \
    iputils-ping \
    python3-dev \
    unzip \
    vim \
    automake \
    libtool \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN git lfs install --skip-smudge

WORKDIR /root

SHELL ["/bin/bash", "-c"]
RUN python3 -m venv venv
RUN source venv/bin/activate && pip install --upgrade pip && pip install wheel
RUN mkdir -p /root/.gen3 && touch /root/.gen3/gen3_client_config.ini

ENV PYTHONUNBUFFERED=1
ENV PATH="/usr/local/bin:$PATH"
COPY ./requirements.txt /root/
RUN source venv/bin/activate && pip install -r requirements.txt


# move these out of requirements.txt, so that builds are quicker when we change
RUN source venv/bin/activate && pip install "gen3_tracker==0.0.7rc27"

RUN source venv/bin/activate && pip install "aced-submission==0.0.10rc18"
RUN source venv/bin/activate && pip install "fhir.resources==7.1.0"

COPY --from=go-builder /go/bin/forge /usr/local/bin/forge
COPY --from=go-builder /go/bin/git-drs /usr/local/bin/git-drs
ENV PATH="/usr/local/bin:$PATH"

RUN ls -la /usr/local/bin/forge
RUN ls -la /usr/local/bin/git-drs

COPY . /root
RUN chmod +x /root/entrypoint.sh
RUN mkdir /root/studies

ENTRYPOINT ["/root/entrypoint.sh"]
CMD ["python", "fhir_import_export.py"]	