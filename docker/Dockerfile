# 只从临时白名单上下文构建，不发送仓库配置或业务数据。
FROM docker:29-cli@sha256:6fae2214c3121a28d5aa639d09032f61310b7c9c75ffbad1025f261a1b621800 AS docker-cli
FROM python:3.12-slim-bookworm
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 xuanpu && useradd --uid 10001 --gid 10001 --create-home xuanpu
COPY requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /build/requirements.txt
COPY wheels/ /build/wheels/
RUN pip install --no-cache-dir --no-deps /build/wheels/*.whl && pip check && rm -rf /build
COPY trial_tickets.py ticket-api.py /opt/xuanpu/
USER 10001:10001
WORKDIR /home/xuanpu
EXPOSE 8420
ENTRYPOINT ["xuanpu"]
CMD ["serve", "--config", "/run/xuanpu/config.json"]
