FROM golang:1.24.0-bookworm AS go-builder

RUN go install github.com/calypr/git-drs@526654610a78fdd587aa66afc1e3dc22b0c155c4

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
RUN mkdir ~/.gen3

ENV PYTHONUNBUFFERED=1
ENV PATH="/usr/local/bin:$PATH"
COPY ./requirements.txt /root/
RUN source venv/bin/activate && pip install -r requirements.txt
RUN source venv/bin/activate && pip install "gen3_tracker==0.0.7rc19" 
RUN source venv/bin/activate && pip install "aced-submission==0.0.10rc13"

COPY --from=go-builder /go/bin/git-drs /usr/local/bin/git-drs

RUN ls -la /usr/local/bin/git-drs

COPY . /root
RUN chmod +x /root/entrypoint.sh
RUN mkdir /root/studies

ENTRYPOINT ["/root/entrypoint.sh"]
CMD ["python", "fhir_import_export.py"]	