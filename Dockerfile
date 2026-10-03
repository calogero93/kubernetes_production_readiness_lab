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

FROM python:3.12-slim-bookworm AS runtime
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

FROM runtime AS local-lab
USER root
ARG TARGETARCH
ARG KIND_VERSION=0.33.0
ARG KUBECTL_VERSION=1.37.0
RUN apt-get update && apt-get install -y --no-install-recommends docker.io curl \
    && curl -fsSLo /tmp/kind "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${TARGETARCH}" \
    && curl -fsSLo /tmp/kind.sha256sum "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${TARGETARCH}.sha256sum" \
    && echo "$(cut -d ' ' -f 1 /tmp/kind.sha256sum)  /tmp/kind" | sha256sum -c - \
    && install -m 0755 /tmp/kind /usr/local/bin/kind \
    && curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c - \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -rf /tmp/kind /tmp/kind.sha256sum /tmp/kubectl /tmp/kubectl.sha256 /var/lib/apt/lists/* \
    && docker --version && kind --version && kubectl version --client

FROM python-builder AS verification
RUN uv sync --frozen --extra ai --extra tracing --extra dev --no-editable
COPY --from=runtime /usr/local/bin/helm /usr/local/bin/helm
ENV PATH="/opt/kubeproof/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/workspace/src
WORKDIR /workspace
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM runtime AS production
