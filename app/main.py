"""FastAPI 入口：API + 内置控制台静态资源 + mock 控制面。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.api.routes import mock as mock_routes
from app.config.constants import API_PREFIX, PROJECT_ROOT
from app.config.settings import get_settings
from app.scheduler import get_scheduler
from app.storage import get_store
from app.utils.clock import fmt_local, utc_now
from app.utils.logging import get_logger, setup_logging

settings = get_settings()
setup_logging()
logger = get_logger(__name__)

STARTED_AT = utc_now()

FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>ai-ops-agent</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:48px;
background:#0b1020;color:#e8ecf6}h1{font-size:22px;margin:0 0 12px}p{color:#a9b4cc;line-height:1.7}
code{background:#161d33;padding:2px 6px;border-radius:4px}a{color:#6ea8fe}</style></head>
<body><h1>ai-ops-agent 后端已启动</h1>
<p>前端控制台尚未构建。执行 <code>cd frontend && npm install && npm run build</code> 后刷新本页即可看到控制台。</p>
<p>API 文档：<a href="/docs">/docs</a>　｜　健康检查：<a href="/healthz">/healthz</a></p>
</body></html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_store()
    logger.info(
        "%s v%s 启动｜mock=%s｜检测周期=%ss｜数据目录=%s",
        settings.app_name,
        settings.version,
        settings.mock_mode,
        settings.detect_interval,
        settings.data_dir,
    )
    await get_scheduler().start()
    try:
        yield
    finally:
        await get_scheduler().stop()


app = FastAPI(
    title="ai-ops-agent",
    version=settings.version,
    description="基于 Python 的轻量级 AI 智能体故障排查工具（骨架版）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=API_PREFIX)
app.include_router(mock_routes.router)


@app.get("/healthz", tags=["meta"], summary="健康检查（容器探针）")
async def healthz() -> JSONResponse:
    scheduler = get_scheduler()
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.app_name,
            "version": settings.version,
            "mock_mode": settings.mock_mode,
            "scheduler_running": scheduler.running,
            "scheduler_rounds": scheduler.rounds,
            "uptime_seconds": round((utc_now() - STARTED_AT).total_seconds(), 1),
            "started_at": fmt_local(STARTED_AT),
        }
    )


@app.get("/", include_in_schema=False)
async def index():
    dist = PROJECT_ROOT / "frontend" / "dist"
    if (dist / "index.html").exists():
        from fastapi.responses import FileResponse

        return FileResponse(dist / "index.html")
    return HTMLResponse(FALLBACK_HTML)


_dist = PROJECT_ROOT / "frontend" / "dist"
if _dist.exists():  # 前端构建产物存在时挂载静态资源
    app.mount("/assets", StaticFiles(directory=_dist / "assets", check_dir=False), name="assets")


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    run()
