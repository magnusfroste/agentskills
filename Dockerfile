FROM python:3.13.1-alpine
WORKDIR /app
COPY app/ /app/
RUN adduser -D -H -u 10001 skills && mkdir -p /data && chown skills /data
USER 10001
ENV DATA_DIR=/data PORT=8000 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=3)"
CMD ["python", "server.py"]
