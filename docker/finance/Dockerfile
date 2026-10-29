FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 finance && useradd --uid 10001 --gid 10001 --create-home finance \
    && mkdir -p /var/lib/finance/originals && chown -R finance:finance /var/lib/finance
COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /build/requirements.lock
COPY packages/ /build/packages/
RUN pip install --no-cache-dir --no-deps /build/packages/xuanpu-spec '/build/packages/xuanpu-finance[s3]' \
    && pip check && rm -rf /build
USER 10001:10001
WORKDIR /home/finance
EXPOSE 8443
ENTRYPOINT ["xuanpu-finance"]
CMD ["--config", "/run/finance/config.yaml"]
