"""FastAPI 应用入口。

职责：启动时准备目录和数据库、注册接口、挂载网页、统一错误处理。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import apps as apps_api
from app.api import chat
from app.api import input as input_api
from app.api import security
from app.api import tasks as tasks_api
from app.api import vision as vision_api
from app.errors import JiaAgentError
from app.logging_setup import setup_logging
from app.memory.db import init_db
from app.settings import WEB_DIR, ensure_dirs, get_secrets, get_tools_config
from app.vision import dpi as vision_dpi
from app.vision import focus as vision_focus
from app.wechat.runtime import start_wechat_runtime, stop_wechat_runtime
from app.workflow.manager import manager as workflow_manager

VERSION = "0.1.0"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动和关闭时要做的事。"""
    ensure_dirs()
    setup_logging(get_secrets().jia_log_level)

    # DPI 感知必须**尽早**设。进程一旦以"未感知"的状态读过屏幕尺寸，
    # 拿到的就是逻辑像素（本机 1707x1067），而截图是物理像素（2560x1600），
    # 两者差 1.5 倍——按窗口矩形去裁剪或标注就会整体错位。
    # 设置失败不阻止启动，但要在日志里说清楚。
    awareness = vision_dpi.ensure_awareness()
    if awareness.get("error"):
        logger.warning("DPI 感知设置有问题：%s", awareness["error"])
    else:
        logger.info(
            "DPI 感知已就绪：%s（%s）",
            awareness.get("awareness"),
            awareness.get("method"),
        )

    init_db()
    # 后台任务的执行体只活在内存里，重启就没了。数据库里残留的"执行中/等待确认"
    # 要标成失败——那些步骤可能有非幂等的副作用（开应用、写文件），绝不自动续跑。
    workflow_manager.reconcile_on_startup()

    # 最近使用的非佳窗口追踪：inspect_screen 的 auto 范围靠它确定目标。
    # 后台线程只读窗口元数据，不截图；服务关闭时一并停止。
    try:
        if get_tools_config().vision.enabled:
            vision_focus.start(get_tools_config().vision)
    except Exception:  # noqa: BLE001
        logger.exception(
            "启动最近窗口追踪失败；auto 范围会明确报错，不会改抓整个桌面"
        )

    # 微信入口默认关闭（JIA_WECHAT_ENABLED）。缺凭证、缺允许名单或 DPAPI
    # 不可用时，它会失败关闭，只记日志，不影响 Jia 的其他功能。
    try:
        await start_wechat_runtime()
    except Exception:  # noqa: BLE001 - 微信是可选入口，不能拖垮主服务
        logger.exception("启动微信入口失败；Jia 其他功能继续运行")

    logger.info("JiaAgent v%s 启动完成，数据库已就绪", VERSION)
    try:
        yield
    finally:
        try:
            await stop_wechat_runtime()
        except Exception:  # noqa: BLE001
            logger.exception("停止微信入口时出错")
        vision_focus.stop()
    logger.info("JiaAgent 已停止")


app = FastAPI(
    title="JiaAgent",
    description="个人 AI Agent（佳）—— 阶段一：聊天 + 记忆 + 多模型",
    version=VERSION,
    lifespan=lifespan,
)

# 本机边界在所有路由之前执行：Host 必须是回环名、连接来源必须是回环
# 地址；/api 的写请求还必须来自同源页面。即使有人误把服务绑到 0.0.0.0，
# 局域网客户端也会在这里被拒绝。详见 app/api/security.py。
app.middleware("http")(security.local_only_middleware)

# 所有后端接口都挂在 /api 下面
app.include_router(chat.router, prefix="/api")
app.include_router(apps_api.router, prefix="/api")
app.include_router(tasks_api.router, prefix="/api")
app.include_router(vision_api.router, prefix="/api")
app.include_router(input_api.router, prefix="/api")


@app.middleware("http")
async def no_cache_for_web(request: Request, call_next):
    """网页文件不强缓存，每次都跟服务器核对一下有没有变化。

    你以后改 web/ 里的 html/css/js，浏览器刷新一下就能看到新效果，
    不会被浏览器缓存住的旧版本骗到（开发阶段特别容易踩这个坑）。
    接口 /api 不受影响。
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
async def health() -> dict:
    """健康检查。浏览器打开这个地址能返回 ok，说明服务活着。"""
    return {"status": "ok", "name": "JiaAgent", "version": VERSION}


@app.exception_handler(JiaAgentError)
async def handle_known_error(request: Request, exc: JiaAgentError) -> JSONResponse:
    """我们自己定义的错误：返回结构化 JSON，不把堆栈甩给用户。"""
    logger.warning("请求 %s 失败：[%s] %s", request.url.path, exc.code, exc.message)
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})


@app.exception_handler(Exception)
async def handle_unknown_error(request: Request, exc: Exception) -> JSONResponse:
    """没预料到的错误：完整写进日志，但只给用户一句简单的话。"""
    logger.exception("请求 %s 出现未预期错误", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "服务器内部错误，详情见 logs/jiaagent.log",
                "detail": str(exc),
            }
        },
    )


# 挂载网页界面。
# 必须放在所有 /api 路由之后，否则 "/" 会把 /api 请求也一起吃掉。
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
