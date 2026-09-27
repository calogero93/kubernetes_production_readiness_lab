FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py /app/app.py

USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python", "/app/app.py"]
CMD ["--host", "0.0.0.0", "--iterations", "20000"]
