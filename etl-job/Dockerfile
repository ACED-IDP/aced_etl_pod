FROM golang:1.26.5-bookworm AS go-builder

WORKDIR /src
COPY go.mod go.sum ./
ENV GOPROXY=direct
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/calypr-etl .

# Git-DRS and Forge are linked as Go libraries; no Python runtime or CLI
# installation is needed in the ETL image.

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    iputils-ping \
    unzip \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root
RUN mkdir -p /root/.gen3 /root/studies /root/repo
RUN touch /root/.gen3/gen3_client_config.ini

ENV PATH="/usr/local/bin:$PATH"
COPY --from=go-builder /out/calypr-etl /usr/local/bin/calypr-etl
RUN ln -s /usr/local/bin/calypr-etl /usr/local/bin/fhir_import_export
COPY entrypoint.sh /root/entrypoint.sh
RUN chmod +x /root/entrypoint.sh

ENTRYPOINT ["/root/entrypoint.sh"]
CMD ["calypr-etl"]
