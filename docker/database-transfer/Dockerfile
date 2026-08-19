FROM postgres:17@sha256:e38411452a464af89e5adadb8d223bf53b898d47d6ef918b2d58c08707350449

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && useradd --uid 1000 --create-home transfer \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY catalogue-dump/pyproject.toml catalogue-dump/sources.json ./
COPY catalogue-dump/src ./src
RUN python3 -m venv /opt/catalogue \
 && /opt/catalogue/bin/pip install --no-cache-dir ".[postgres]"

ENV PATH=/opt/catalogue/bin:/usr/lib/postgresql/17/bin:/usr/local/bin:/usr/bin:/bin
ENTRYPOINT ["catalogue-db-transfer"]
