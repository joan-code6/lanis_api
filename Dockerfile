FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 lanis \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin lanis \
    && mkdir -p /app/data \
    && chown lanis:lanis /app/data

COPY api ./api
COPY schulportal_hessen ./schulportal_hessen
COPY sph_client ./sph_client

ENV LANIS_API_HOST=0.0.0.0
ENV LANIS_API_PORT=8000
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

USER 10001:10001

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "api.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
