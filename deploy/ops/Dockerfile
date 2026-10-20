# M8b ops image: the frozen API image plus age encryption (pyrage, Python
# bindings of the maintained rage implementation of age-encryption.org/v1).
# Version and wheel hashes are pinned; nothing else is added. Build with
#   docker compose ... --profile ops build ops   (RAS_API_IMAGE must exist locally)
ARG RAS_API_IMAGE
FROM ${RAS_API_IMAGE}
USER root
COPY requirements.txt /tmp/ops-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r /tmp/ops-requirements.txt \
    && rm /tmp/ops-requirements.txt \
    && mkdir -p /var/lib/ras/staging /var/lib/ras/ops-state && chown ras /var/lib/ras/staging /var/lib/ras/ops-state
USER ras
CMD ["python", "-m", "app.cli.ops", "preflight"]
