# haiqbot 运行时镜像
#
# 只打包框架本身：内核 + 框架自带插件（plugins/）+ 你自己的插件（plugins-extra/）。
# 协议端（NapCat / Lagrange / LLOneBot …）不在这里，也不会在这里 —— 见 docs/DEPLOY.md。
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HAIQ_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

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
