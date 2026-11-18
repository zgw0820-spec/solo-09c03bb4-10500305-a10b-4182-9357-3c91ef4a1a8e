# 通用 FastAPI 服务镜像，通过构建参数 APP_DIR 选择 proxy / backend / web
# 构建上下文固定为仓库根目录，三个服务共用本文件。
FROM python:3.11-slim

ARG APP_DIR=proxy
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY ${APP_DIR}/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY ${APP_DIR}/app /srv/app

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
