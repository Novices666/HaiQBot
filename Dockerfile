# haiqbot 运行时镜像
# 只打包框架本身：内核 + 框架自带插件（plugins/）+ 你自己的插件（plugins-extra/）。
# 协议端（NapCat / Lagrange / LLOneBot …）不在这里，也不会在这里 —— 见 docs/DEPLOY.md。
#
# 大陆网络三个开关（默认值 = 原样不动，不影响其它地区）：
#   BASE_IMAGE    换 Docker Hub 加速器，例如 docker.m.daocloud.io/library/python:3.12-slim
#   APT_MIRROR    只换主机名（保留 /debian 与 /debian-security 路径），例如 mirrors.aliyun.com
#   UV_INDEX_URL  PyPI 镜像；给了它就**不用** astral.sh 装 uv，改为 pip 从镜像装
# 用法见 compose.cn.yaml；不传这些参数时，构建行为与本文件之前完全一致。
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HAIQ_DATA_DIR=/data

ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""
ARG UV_INDEX_URL=""
# uv 认 UV_DEFAULT_INDEX（新名）与 UV_INDEX_URL（旧名），两个都设，省得被版本差异坑
ENV UV_INDEX_URL=${UV_INDEX_URL} \
    UV_DEFAULT_INDEX=${UV_INDEX_URL}

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
            if [ -f "$f" ]; then \
                sed -i \
                    -e "s|//deb.debian.org|//${APT_MIRROR}|g" \
                    -e "s|//security.debian.org|//${APT_MIRROR}|g" "$f"; \
            fi; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# 官方安装脚本走 astral.sh（大陆常被拖慢/卡住）；给了 PyPI 镜像就从镜像装 uv
RUN set -eux; \
    if [ -n "$PIP_INDEX_URL" ]; then \
        python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" uv; \
    else \
        curl -LsSf https://astral.sh/uv/install.sh | sh; \
        ln -s /root/.local/bin/uv /usr/local/bin/uv; \
    fi

WORKDIR /app

# 先只装依赖，最大化利用镜像层缓存
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# 插件不打进内核 wheel：随镜像放到 /app/{plugins,plugins-extra}，由 plugin_dirs 发现。
# 两个目录缺一个，对应的插件就整组消失（且不会有任何报错）。
COPY plugins ./plugins
COPY plugins-extra ./plugins-extra
RUN uv sync --no-dev --extra official

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /data /secrets && useradd -m -u 10001 haiqbot && chown -R haiqbot:haiqbot /app /data /secrets
USER haiqbot

EXPOSE 8080
VOLUME ["/data", "/secrets"]

# 健康检查只能打免登录路径：/api/* 全部要会话（含 /api/overview），401 会被当成不健康。
# "/" 是面板首页（未登录也返回 200），用来判断进程是否在监听。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/',timeout=3).status==200 else 1)"

# 控制台脚本叫 haiq（pyproject 的 [project.scripts]），没有 haiqbot 这个命令
ENTRYPOINT ["haiq"]
CMD ["run"]
