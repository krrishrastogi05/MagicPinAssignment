FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VERA_DATABASE_PATH=/data/vera.db \
    PORT=8080

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN addgroup --system vera && adduser --system --ingroup vera vera \
    && mkdir -p /data && chown -R vera:vera /app /data

USER vera
EXPOSE 8080

HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import json,os,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:{}/v1/healthz'.format(os.getenv('PORT','8080')), timeout=2))['status']=='ok'"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1 --no-access-log"]
