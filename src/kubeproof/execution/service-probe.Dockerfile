FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY service_probe_runner.py /app/kubeproof/execution/service_probe_runner.py
USER 10001:10001
CMD ["python", "-m", "kubeproof.execution.service_probe_runner"]
