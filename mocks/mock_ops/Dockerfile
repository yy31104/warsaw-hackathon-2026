FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/backend/
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
RUN cd backend && uv sync --frozen --no-dev --no-install-project
COPY mocks/mock_ops/service.py /app/service.py
ENV MOCK_OPS_DB=/data/mock-ops.sqlite3
RUN mkdir -p /data
EXPOSE 8200
CMD ["/app/backend/.venv/bin/uvicorn","service:app_factory","--factory","--host","0.0.0.0","--port","8200"]
