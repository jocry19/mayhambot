# Option für später: mayhambot auf einem Linux-Server. Build-Kontext = mayhambot/
ARG BASE=python:3.12-slim
FROM ${BASE}
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt pytest
COPY . .
RUN rm -rf .env data logs .venv && mkdir -p data logs \
 && useradd -u 1000 -m mayham && chown -R mayham /app/data /app/logs
USER mayham
EXPOSE 8501
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
  CMD python deploy/ops.py health || exit 1
CMD ["python", "mayham.py", "run"]
