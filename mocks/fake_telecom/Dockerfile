FROM python:3.12-slim

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    FAKE_TELECOM_DB=/data/receipts.sqlite3

WORKDIR /deps
COPY backend/pyproject.toml backend/uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project --no-cache

WORKDIR /app
COPY backend/alerting/clock.py ./alerting/clock.py
COPY mocks/fake_telecom/app.py mocks/fake_telecom/inbox.py mocks/fake_telecom/index.html ./mocks/fake_telecom/
RUN useradd --system --create-home gateway && install -d -o gateway -g gateway /data
USER gateway
EXPOSE 8100
CMD ["uvicorn", "mocks.fake_telecom.app:app", "--host", "0.0.0.0", "--port", "8100"]
