# HostBot - VPS Docker image (Python 3.14)
FROM python:3.14-slim

# Node.js is required to run uploaded .js scripts
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot/ /app/

# Persist uploads + data outside the container image (MongoDB holds the rest)
ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 9090

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request,sys; u=os.environ.get('STATUS_SERVER_ENABLED','false').lower()=='true'; sys.exit(0 if (not u or urllib.request.urlopen('http://127.0.0.1:9090/health', timeout=3).status==200) else 1)"

CMD ["python", "hostbot.py"]