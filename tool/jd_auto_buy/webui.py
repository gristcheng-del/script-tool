"""
京东自动购买 — Web UI 服务器
===========================
FastAPI 服务，提供 REST API + SSE 日志流 + 单页前端。
用法: python webui.py [--port 8080] [--config config.json]
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# 必须在导入 web_runner 之前，因为它会设置日志
_script_dir = Path(__file__).parent.resolve()

# ── 导入桥梁层 ──
from web_runner import LogCaptureManager, MonitorRunner
from sse_starlette.sse import EventSourceResponse

# ── 初始化 ──
log_capture = LogCaptureManager()
runner: MonitorRunner = None  # 在 startup 事件中初始化

logger = logging.getLogger("webui")

# ── FastAPI 应用 ──
app = FastAPI(
    title="JD Auto-Buy Web UI",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
)

# ═══════════════════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════════════════

class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=5)
    enabled: bool = True
    size: str = ""
    color: str = ""
    max_price: float = 0.0
    quantity: int = Field(default=1, ge=1)

class ProductUpdate(BaseModel):
    name: str = None
    url: str = None
    enabled: bool = None
    size: str = None
    color: str = None
    max_price: float = None
    quantity: int = None

class MonitorStartRequest(BaseModel):
    dry_run: bool = False

class RestockStartRequest(BaseModel):
    time: str = Field(default="", min_length=0)  # 允许空字符串，路由中再校验
    advance: int = Field(default=30, ge=15)
    dry_run: bool = False
    product_id: int = None

class SettingsUpdate(BaseModel):
    schedule: dict = None
    browser: dict = None
    checkout: dict = None
    selectors: dict = None

# ═══════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════

def _ok(data=None, message: str = ""):
    return JSONResponse({"success": True, "data": data, "message": message})

def _err(message: str, status: int = 400, error_code: str = ""):
    return JSONResponse(
        {"success": False, "message": message, "error_code": error_code},
        status_code=status,
    )

# ── 状态 ──
@app.get("/api/status")
async def get_status():
    return _ok(data=runner.status)

# ── 配置 ──
@app.get("/api/config")
async def get_config():
    return _ok(data=runner.load_config())

@app.post("/api/config")
async def update_config(config: dict):
    if runner.save_config(config):
        return _ok(message="配置已保存")
    return _err("保存配置失败")

# ── 产品 CRUD ──
@app.get("/api/products")
async def get_products():
    products = runner.get_products()
    return _ok(data={"products": products, "count": len(products)})

@app.post("/api/products")
async def add_product(product: ProductCreate):
    try:
        new_id = runner.add_product(product.model_dump())
        return _ok(data={"id": new_id}, message=f"已添加: {product.name}")
    except Exception as e:
        return _err(str(e))

@app.put("/api/products/{product_id}")
async def update_product(product_id: int, product: ProductUpdate):
    data = {k: v for k, v in product.model_dump().items() if v is not None}
    if not data:
        return _err("没有需要更新的字段")
    if runner.update_product(product_id, data):
        return _ok(message="商品已更新")
    return _err("商品不存在", status=404)

@app.delete("/api/products/{product_id}")
async def delete_product(product_id: int):
    if runner.delete_product(product_id):
        return _ok(message="商品已删除")
    return _err("商品不存在", status=404)

# ── 监控控制 ──
@app.post("/api/monitor/start")
async def start_monitor(req: MonitorStartRequest):
    try:
        ok = runner.start_monitoring(dry_run=req.dry_run)
        if not ok:
            return _err("已有任务在运行", status=409, error_code="ALREADY_RUNNING")
        return _ok(message="监控已启动")
    except ValueError as e:
        return _err(str(e), error_code="NO_PRODUCTS")

@app.post("/api/monitor/stop")
async def stop_monitor():
    if runner.stop():
        return _ok(message="监控已停止")
    return _err("没有正在运行的任务", status=409, error_code="NOT_RUNNING")

@app.post("/api/monitor/check-now")
async def check_now():
    if runner.trigger_check_now():
        return _ok(message="立即检查已触发")
    return _err("监控未在运行", status=409)

@app.post("/api/monitor/check-once")
async def check_once(req: MonitorStartRequest):
    try:
        ok = runner.run_check_once(dry_run=req.dry_run)
        if not ok:
            return _err("已有任务在运行", status=409)
        return _ok(message="单次检查已启动")
    except ValueError as e:
        return _err(str(e), error_code="NO_PRODUCTS")

# ── 抢购控制 ──
@app.post("/api/restock/start")
async def start_restock(req: RestockStartRequest):
    if not req.time or len(req.time.strip()) < 4:
        return _err("请填写有效的目标时间（格式 HH:MM 或 HH:MM:SS）")
    try:
        ok = runner.start_restock(
            target_time=req.time.strip(),
            advance=req.advance,
            dry_run=req.dry_run,
            product_id=req.product_id,
        )
        if not ok:
            return _err("已有任务在运行", status=409, error_code="ALREADY_RUNNING")
        return _ok(message=f"定时抢购已启动，目标时间: {req.time}")
    except ValueError as e:
        return _err(str(e), error_code="INVALID_PARAMS")

@app.post("/api/restock/stop")
async def stop_restock():
    if runner.stop():
        return _ok(message="抢购已取消")
    return _err("没有正在运行的抢购任务", status=409)

# ── 日志 ──
@app.get("/api/logs")
async def get_logs(limit: int = Query(default=100, ge=1, le=500),
                   level: str = Query(default=None)):
    logs = log_capture.get_recent_logs(limit=limit, level=level)
    return _ok(data={"logs": logs, "total": len(logs), "limit": limit})

@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    """SSE 实时日志流"""
    client_id = str(id(request))
    q = log_capture.register_client(client_id)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    data = json.loads(msg)
                    yield {"event": data["type"], "data": json.dumps(data["data"], ensure_ascii=False)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        except asyncio.CancelledError:
            pass
        finally:
            log_capture.unregister_client(client_id)

    return EventSourceResponse(event_generator())

# ── 设置 ──
@app.get("/api/settings")
async def get_settings():
    config = runner.load_config()
    return _ok(data={
        "schedule": config.get("schedule", {}),
        "browser": config.get("browser", {}),
        "checkout": config.get("checkout", {}),
    })

@app.put("/api/settings")
async def update_settings(settings: SettingsUpdate):
    runner.update_settings(settings.model_dump(exclude_none=True))
    return _ok(message="设置已保存")


# ═══════════════════════════════════════════════════════════
# 静态文件
# ═══════════════════════════════════════════════════════════

@app.get("/")
async def serve_index():
    return FileResponse(_script_dir / "static" / "index.html")


# ═══════════════════════════════════════════════════════════
# 启动 / 关闭
# ═══════════════════════════════════════════════════════════

_config_path = "config.json"

@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    log_capture.setup(loop)
    global runner
    runner = MonitorRunner(config_path=_config_path, log_capture=log_capture)
    # 修复旧数据（无 id 的产品自动补上）
    runner._repair_product_ids()
    # 同步产品 ID 计数器
    config = runner.load_config()
    products = config.get("products", [])
    if products:
        max_id = max((p.get("id", 0) for p in products), default=0)
        runner._next_product_id = max_id + 1
        logger.info(f"产品 ID 计数器: {runner._next_product_id}")
    logger.info("JD Auto-Buy Web UI 已启动")

@app.on_event("shutdown")
async def shutdown():
    if runner:
        runner.stop()
    logger.info("Web UI 已关闭")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

def main():
    global _config_path

    parser = argparse.ArgumentParser(description="JD Auto-Buy Web UI")
    parser.add_argument("--port", "-p", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    _config_path = args.config

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
