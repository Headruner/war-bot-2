# ---------------------------------------------------------------------------------------------
# EVE Distance Meter - one small Python process (FastAPI + uvicorn) with a one-file SQLite
# database (WAL) on the /app/data volume. Sized for a 2 vCPU / 1 GB RAM VM: idle RSS ~75 MB.
#
# Multi-stage: the build stage installs the pinned requirements.txt into a virtualenv; the runtime
# stage copies only that virtualenv and runs as the non-root user 10001.
# Default base image: python:3.12-slim. Any Debian/Ubuntu image with python3 + venv also works:
#   docker build --build-arg BASE_IMAGE=<image> -t evedm .
# Runs on Linux and on Windows/macOS through Docker Desktop (Linux containers).
# ---------------------------------------------------------------------------------------------
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python3 -m venv /opt/evedm
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
# Optional build secret "pipca": a CA bundle for builds behind a TLS-inspecting proxy
#   docker build --secret id=pipca,src=/path/to/ca.crt ...   (not needed on a normal network)
RUN --mount=type=secret,id=pipca,required=false \
    if [ -f /run/secrets/pipca ]; then export PIP_CERT=/run/secrets/pipca; fi \
 && /opt/evedm/bin/pip install --upgrade pip setuptools wheel \
 && /opt/evedm/bin/pip install -r requirements.txt \
 && /opt/evedm/bin/pip install --no-deps . \
 && /opt/evedm/bin/pip check \
 && /opt/evedm/bin/pip uninstall -y pip setuptools wheel \
 && find /opt/evedm -name '__pycache__' -prune -exec rm -rf {} + \
 && /opt/evedm/bin/python -m compileall -q /opt/evedm/lib

FROM ${BASE_IMAGE}
ARG VERSION=0.3.0
ENV PATH=/opt/evedm/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # fewer glibc malloc arenas -> lower resident memory for a mostly idle async process
    MALLOC_ARENA_MAX=2 \
    EVEDM_DATA_DIR=/app/data \
    EVEDM_HOST=0.0.0.0 \
    EVEDM_PORT=8080 \
    EVEDM_CONFIG=/app/config/evedm.yaml \
    EVEDM_SQLITE_JOURNAL=wal
RUN groupadd --gid 10001 evedm \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin evedm \
 && mkdir -p /app/data /app/config \
 && chown 10001:10001 /app/data
COPY --from=build /opt/evedm /opt/evedm
COPY config/evedm.example.yaml /app/config/evedm.yaml
# war watcher settings (separate file; mount your own over it or use EVEDM_WARS_* variables)
COPY config/wars.yaml /app/config/wars.yaml
WORKDIR /app
USER 10001:10001
# verify dependencies and bundled resources inside the final image
RUN evedm check --no-data
# One-file SQLite database (WAL), logs and launch records: mount a volume or a host folder here.
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["evedm", "healthcheck"]
LABEL org.opencontainers.image.title="evedm" \
      org.opencontainers.image.description="EVE Distance Meter: rank EVE characters by stargate jumps, route on a 2D star map" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
ENTRYPOINT ["evedm"]
CMD ["serve"]
