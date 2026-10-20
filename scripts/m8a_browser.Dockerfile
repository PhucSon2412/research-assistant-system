# M8a smoke only: Google Chrome stable on Linux with NSS tools, so the smoke can
# trust its throwaway test CA in a per-run NSS database (real chain and
# hostname verification; no --ignore-certificate-errors). Never a service image.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
RUN apt-get update \
    && apt-get install -y --no-install-recommends libnss3-tools \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages playwright==1.63.0 \
    && python3 -m playwright install --with-deps chrome
