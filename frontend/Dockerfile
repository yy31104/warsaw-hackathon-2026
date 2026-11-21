FROM node:22-alpine AS build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
COPY frontend/demo ./demo
COPY config ../config
COPY fixtures/api ../fixtures/api
COPY loadtest/results ../loadtest/results
ENV VITE_MOCK=0 VITE_ALERTS_MODE=api
RUN npm run build

FROM nginx:1.28-alpine
COPY --from=build /app/frontend/dist /usr/share/nginx/html
RUN <<'EOF'
cat >/etc/nginx/conf.d/default.conf <<'NGINX'
server {
    listen 5173;
    root /usr/share/nginx/html;

    location /v1/ {
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $backend api:8000;
        proxy_pass http://$backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 60s;
    }

    location = /ops-health {
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $backend api:8000;
        proxy_pass http://$backend/health;
    }

    location = /metrics {
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $backend api:8000;
        proxy_pass http://$backend/metrics;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINX
EOF
EXPOSE 5173
