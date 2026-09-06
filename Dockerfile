# ATLAS V2 runtime image.
#
# Runs as a non-root user with no build toolchain in the final layer: the process needs
# to make HTTPS requests and write one SQLite file, nothing more.
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

# Non-root. A trading process has no reason to be able to write outside its data dir.
RUN useradd --create-home --shell /usr/sbin/nologin atlas

COPY --from=builder /install /usr/local
WORKDIR /app
COPY scripts ./scripts

# Persistent state lives here and must be a mounted volume, or a container restart
# loses the ledger, the audit chain and the kill-switch state.
ENV ATLAS_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data && chown atlas:atlas /data
VOLUME ["/data"]

USER atlas

# Reports unhealthy when the heartbeat is stale, so an orchestrator can restart a hung
# process. Exit code only - no output, so nothing can leak into container logs.
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD ["atlas", "health", "--quiet"]

# No CMD default of `run`: starting a trading process must be a deliberate act by
# whoever launches the container, not an image default.
ENTRYPOINT ["atlas"]
CMD ["--help"]
