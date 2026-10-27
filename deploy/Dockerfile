# syntax=docker/dockerfile:1
# 多阶段构建：前端静态资源在 node 阶段产出，运行镜像只保留 Python 运行时与产物

# 前端产物与目标架构无关，固定在构建机架构（$BUILDPLATFORM）上跑
# 否则 buildx 多架构构建时，arm64 那一路会把 npm install + vite 构建放进 QEMU 模拟，慢一个数量级
FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --registry=https://registry.npmmirror.com --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    PORT=8080

# 配置发现顺序：CONFIG_FILE → CONFIG_DIR/aiops.yaml → /etc/aiops/aiops.yaml → /app/config/aiops.yaml
# 所以 -v $(pwd)/config:/etc/aiops:ro 挂上去就会自动生效，不用额外加环境变量

WORKDIR /app

# 依赖清单在 deploy/ 下，构建时拷进来用完即删，不留在运行镜像里
COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -f requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown -R appuser:appuser /data /app

COPY app ./app
# 配置内置一份默认值（/app/config），运行时用 -v 挂载到 /etc/aiops 即可整体覆盖
COPY config ./config
COPY --from=frontend-build /build/dist ./frontend/dist

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "app.main"]
