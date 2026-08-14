FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

COPY pyproject.toml README.md ./
COPY contract_review ./contract_review
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home app \
    && mkdir -p /data \
    && chown app:app /data
# Copy application sources again after installation. This ensures setuptools
# cannot replace freshly edited static assets with stale package build output.
COPY contract_review ./contract_review

USER app

CMD ["uvicorn", "contract_review.main:app", "--host", "0.0.0.0", "--port", "8000"]
