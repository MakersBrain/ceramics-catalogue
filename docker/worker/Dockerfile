# The collection worker.
#
# Two images are built from this one file, and the difference is the whole
# reason capabilities exist (§5.5): camoufox is most of the image and most of
# the memory, and only two of the eighty sources need it.
#
#   docker build --target worker          -> the plain worker
#   docker build --target worker-browser  -> `--capabilities browser`
#
# The plain worker must not inherit the browser's dependency set, which is why
# the extras are declared separately in pyproject.toml rather than as one
# dependency list.

FROM python:3.12-slim AS base

# libpq is not needed — psycopg[binary] carries its own — but ca-certificates
# is, or every https fetch fails with a verification error that looks like the
# shop's fault.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY catalogue-dump/pyproject.toml catalogue-dump/sources.json ./
COPY catalogue-dump/src ./src

RUN useradd --system --uid 10001 --create-home catalogue

# Shared volumes. The cache is per-host sharded so two workers never write one
# entry; the dumps are namespaced <run-id>/<job-id>/ so two runs of one source
# never write one path.
ENV CATALOGUE_CACHE_DIR=/var/lib/catalogue/cache \
    CATALOGUE_DUMPS_DIR=/var/lib/catalogue/dumps \
    CATALOGUE_LOG_JSON=true
RUN mkdir -p /var/lib/catalogue/cache /var/lib/catalogue/dumps \
 && chown -R catalogue /var/lib/catalogue


FROM base AS worker

RUN pip install --no-cache-dir ".[postgres,otel]" && rm -rf /root/.cache
USER catalogue
WORKDIR /var/lib/catalogue
# SIGTERM is handled as a drain: it finishes the current source, requeues
# anything unfinished and marks the worker stopped. Give it room to do that
# rather than the default ten seconds.
STOPSIGNAL SIGTERM
ENTRYPOINT ["catalogue-worker"]


FROM base AS worker-browser

# camoufox pulls a full browser. Kept in its own stage so the plain worker's
# image does not carry it.
RUN pip install --no-cache-dir ".[postgres,otel,browser]" \
 && python -c "import camoufox" \
 && rm -rf /root/.cache
USER catalogue
WORKDIR /var/lib/catalogue

# pip brings the launcher, not the browser: `camoufox fetch` downloads that
# separately. Without it `import camoufox` still succeeds — which is why the
# check above was not enough — and every render fails at runtime with
# "official/stable is not installed", on an image whose entrypoint advertises
# `--capabilities browser`.
#
# It runs after `USER catalogue` on purpose: the download lands in
# $HOME/.cache/camoufox, so fetching as root would put it in a directory this
# process cannot read and leave the image just as broken.
RUN python -m camoufox fetch \
 && python -c "from camoufox.pkgman import installed_verstr; print(installed_verstr())"

STOPSIGNAL SIGTERM
ENTRYPOINT ["catalogue-worker", "--capabilities", "browser"]
