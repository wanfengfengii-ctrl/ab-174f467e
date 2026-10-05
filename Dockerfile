# syntax=docker/dockerfile:1
#
# 深空测控时间归一化服务 —— 零第三方依赖（仅 Python 3.11 标准库）。
# 多阶段：
#   api     运行镜像（非 root，内置 HEALTHCHECK）
#   verify  一次性校验镜像，等待 API 健康后跑测试 / 构建检查 / 跨闰秒冒烟

FROM python:3.11-slim-bookworm AS api

LABEL org.opencontainers.image.title="deep-space-time-normalizer" \
      org.opencontainers.image.description="GPS/UTC -> TAI nanosecond normalization over leap seconds" \
      org.opencontainers.image.source="local"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY app/ ./app/

# 构建哨兵：对随镜像发布的源码取 sha256，verify 服务据此确认
# “正在响应健康检查的 API”与“verify 自身”出自同一个成功构建的镜像。
RUN (find app -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1) > IMAGE_BUILD_SHA

RUN chown -R app:app /app
USER app
EXPOSE 8080

# 标准库实现的健康检查，不依赖 curl/wget。
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=12 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "app.server"]

# ---------------------------------------------------------------------------
FROM api AS verify

# verify 阶段不监听端口：禁用继承自 api 的 HEALTHCHECK，
# 避免一次性容器被标记为无意义的 unhealthy（不影响其退出码）。
HEALTHCHECK NONE

# verify 阶段继承 api 镜像 —— 能构建本阶段即证明 api 镜像可成功构建。
USER root
COPY verify/ ./verify/
COPY tests/ ./tests/
RUN chown -R app:app /app
USER app

# 等待 API 健康 -> 代码测试 -> 镜像构建检查 -> 跨闰秒 HTTP 冒烟，
# 以退出码报告结论后自行退出。
CMD ["python", "-m", "verify.run_verify"]
