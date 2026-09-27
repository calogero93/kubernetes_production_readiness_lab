# syntax=docker/dockerfile:1.7
FROM node:24-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS python-builder
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_PROJECT_ENVIRONMENT=/opt/kubeproof/.venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --extra ai --extra tracing --no-editable

FROM python:3.12-slim-bookworm
ARG TARGETARCH
ARG HELM_VERSION=3.21.1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && tar -xzf /tmp/helm.tar.gz -C /tmp "linux-${TARGETARCH}/helm" \
    && install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz "/tmp/linux-${TARGETARCH}" /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove curl
RUN groupadd --system --gid 10001 kubeproof \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/kubeproof --create-home kubeproof \
    && mkdir -p /data \
    && chown 10001:10001 /data
COPY --from=python-builder /opt/kubeproof/.venv /opt/kubeproof/.venv
COPY --from=frontend /frontend/dist /opt/kubeproof/frontend
ENV PATH="/opt/kubeproof/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["kubeproof", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data", "--frontend-dir", "/opt/kubeproof/frontend"]
