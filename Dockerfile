# haiqbot 运行时镜像
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    WEFT_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# 先只装依赖，最大化利用镜像层缓存
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev --extra official

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /data /secrets && useradd -m -u 10001 haiqbot && chown -R haiqbot:haiqbot /app /data /secrets
USER haiqbot

EXPOSE 8080
VOLUME ["/data", "/secrets"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/status',timeout=3).status==200 else 1)"

ENTRYPOINT ["haiqbot"]
CMD ["run"]
