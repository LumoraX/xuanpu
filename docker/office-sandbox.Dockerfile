# 使用独立白名单上下文：仅本文件和办公依赖锁，禁止发送平台数据与密钥。
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENPYXL_DEFUSEDXML=True
COPY office-requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /build/requirements.lock && pip check && rm -rf /build
# 守护进程由taifeng-sandbox经只读引导注入，不在镜像内安装平台服务。
WORKDIR /workspace
CMD ["python3", "-c", "import time; time.sleep(2147483647)"]
