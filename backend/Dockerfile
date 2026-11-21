FROM python:3.12-slim

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project --no-cache
COPY backend/alerting ./alerting
COPY fixtures/protected ./fixtures/protected
RUN useradd --system --create-home alerting
USER alerting
