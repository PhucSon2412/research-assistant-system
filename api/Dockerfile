FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/api

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --system --uid 10001 --no-create-home ras     && mkdir -p /var/lib/ras/journal && chown ras /var/lib/ras/journal
USER ras

EXPOSE 8000
# M8a: the client address is the TCP peer; uvicorn never rewrites it from
# X-Forwarded-For. Auth rate limits read X-RAS-Client-IP only from the
# configured proxy address (RAS_HTTP__TRUSTED_PROXY_CIDRS).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
