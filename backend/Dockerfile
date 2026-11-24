# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS deps
# Runtime libs for mssql-python's bundled ODBC driver (Fabric SQL endpoint)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

FROM deps AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY . .
RUN ruff check . && pytest

FROM deps AS runtime
# non-root, read-only friendly: only /app/var is written (audit db in local mode)
RUN useradd --uid 10001 --create-home appuser
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser knowledge ./knowledge
COPY --chown=appuser:appuser data ./data
COPY --chown=appuser:appuser scripts ./scripts
RUN mkdir -p /app/var && chown appuser:appuser /app/var
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live').status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
