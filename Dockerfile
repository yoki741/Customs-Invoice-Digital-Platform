# ============================================================
# 报关单数字化平台（Customs-Invoice-Digital-Platform）
# 以子路径 /customs-invoice/ 接入 DTD_nginx Portal 的容器镜像。
#
# 构建：docker build -t dkt-customs-invoice:latest .
# 说明：身份由 Portal 的 Nginx + oauth2-proxy 通过 Remote-* header 注入，
#       容器内不监听公网、不启用本地口令登录（CUSTOMS_AUTH_MODE=sso）。
# ============================================================

# ---- 阶段 1：前端 Univer 表格组件（esbuild 打包，产物不入库）----
FROM node:20-slim AS frontend
WORKDIR /src/webapp/static_src
COPY webapp/static_src/package.json webapp/static_src/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webapp/static_src/ ./
RUN node build.mjs

# ---- 阶段 2：Python 运行时 ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    APP_PREFIX=/customs-invoice \
    CUSTOMS_AUTH_MODE=sso \
    CUSTOMS_SEED_ADMIN=0

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 业务代码与导出模板（模板缺失会导致导出失败）
COPY core ./core
COPY webapp ./webapp
COPY 模板文件 ./模板文件
COPY ["Eastex Vanco CDC 布料单价.xlsx", "./"]

# 前端产物覆盖到静态目录（univer.js / univer.css / assets/）
COPY --from=frontend /src/webapp/static/vendor/ ./webapp/static/vendor/

# 运行数据（SQLite、上传源文件、导出结果）挂载卷持久化
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app

USER app

EXPOSE 8000
VOLUME ["/app/data"]

# 健康检查走 /healthz（免鉴权，容器内 127.0.0.1 属于可信来源）
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# 不启用 --proxy-headers：request.client 保持为 Nginx 容器地址，
# 这样 TrustedProxyMiddleware 才能按「可信来源 IP」校验 Remote-* 身份头。
CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
