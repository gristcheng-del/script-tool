"""
大麦网自动抢票 — Web UI
======================
用法: python webui.py [--port 8081] [--config config.json]
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

_script_dir = Path(__file__).parent.resolve()

from web_runner import LogCaptureManager, DamaiRunner

log_capture = LogCaptureManager()
runner: DamaiRunner = None

logger = logging.getLogger("damai_webui")

app = FastAPI(title="Damai Auto-Buy", version="1.0.0", docs_url=None, redoc_url=None)

# ═══════════════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════════════

class EventCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=5)
    enabled: bool = True
    tier: str = ""
    quantity: int = Field(default=1, ge=1)

class EventUpdate(BaseModel):
    name: str = None
    url: str = None
    enabled: bool = None
    tier: str = None
    quantity: int = None

class BuyStartRequest(BaseModel):
    time: str = Field(..., min_length=4)
    advance: int = Field(default=30, ge=15)
    dry_run: bool = False
    event_id: int = None

class CheckOnceRequest(BaseModel):
    dry_run: bool = False

class SettingsUpdate(BaseModel):
    schedule: dict = None
    browser: dict = None
    checkout: dict = None
    selectors: dict = None

# ═══════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════

def _ok(data=None, message: str = ""):
    return JSONResponse({"success": True, "data": data, "message": message})

def _err(message: str, status: int = 400, error_code: str = ""):
    return JSONResponse(
        {"success": False, "message": message, "error_code": error_code},
        status_code=status,
    )

# ═══════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════

@app.get("/api/status")
async def get_status():
    return _ok(data=runner.status)

@app.get("/api/events")
async def get_events():
    events = runner.get_events()
    return _ok(data={"events": events, "count": len(events)})

@app.post("/api/events")
async def add_event(event: EventCreate):
    try:
        new_id = runner.add_event(event.model_dump())
        return _ok(data={"id": new_id}, message=f"已添加: {event.name}")
    except Exception as e:
        return _err(str(e))

@app.put("/api/events/{event_id}")
async def update_event(event_id: int, event: EventUpdate):
    data = {k: v for k, v in event.model_dump().items() if v is not None}
    if not data:
        return _err("没有需要更新的字段")
    if runner.update_event(event_id, data):
        return _ok(message="演出已更新")
    return _err("演出不存在", status=404)

@app.delete("/api/events/{event_id}")
async def delete_event(event_id: int):
    if runner.delete_event(event_id):
        return _ok(message="演出已删除")
    return _err("演出不存在", status=404)

@app.get("/api/config")
async def get_config():
    return _ok(data=runner.load_config())

@app.post("/api/config")
async def update_config(config: dict):
    if runner.save_config(config):
        return _ok(message="配置已保存")
    return _err("保存配置失败")

@app.post("/api/buy/start")
async def start_buy(req: BuyStartRequest):
    try:
        ok = runner.start_buy(
            target_time=req.time, advance=req.advance,
            dry_run=req.dry_run, event_id=req.event_id,
        )
        if not ok:
            return _err("已有任务在运行", status=409, error_code="ALREADY_RUNNING")
        return _ok(message=f"抢票已启动，目标时间: {req.time}")
    except ValueError as e:
        return _err(str(e), error_code="INVALID_PARAMS")

@app.post("/api/buy/stop")
async def stop_buy():
    if runner.stop():
        return _ok(message="已停止")
    return _err("没有正在运行的任务", status=409)

@app.post("/api/buy/check-once")
async def check_once(req: CheckOnceRequest):
    try:
        ok = runner.run_check_once(dry_run=req.dry_run)
        if not ok:
            return _err("已有任务在运行", status=409)
        return _ok(message="单次检查已启动")
    except ValueError as e:
        return _err(str(e), error_code="NO_EVENTS")

@app.get("/api/logs")
async def get_logs(limit: int = Query(default=100, ge=1, le=500),
                   level: str = Query(default=None)):
    logs = log_capture.get_recent_logs(limit=limit, level=level)
    return _ok(data={"logs": logs, "total": len(logs), "limit": limit})

@app.get("/api/logs/stream")
async def stream_logs(request: Request):
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

# ═══════════════════════════════════════════════════════
# 静态文件
# ═══════════════════════════════════════════════════════

@app.get("/")
async def serve_index():
    return FileResponse(_script_dir / "static" / "index.html")


_config_path = "config.json"

@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    log_capture.setup(loop)
    global runner
    runner = DamaiRunner(config_path=_config_path, log_capture=log_capture)
    config = runner.load_config()
    events = config.get("events", [])
    if events:
        max_id = max((e.get("id", 0) for e in events), default=0)
        runner._next_event_id = max_id + 1
    logger.info("大麦自动抢票 Web UI 已启动")

@app.on_event("shutdown")
async def shutdown():
    if runner:
        runner.stop()
    logger.info("Web UI 已关闭")


def main():
    global _config_path
    parser = argparse.ArgumentParser(description="大麦自动抢票 Web UI")
    parser.add_argument("--port", "-p", type=int, default=8081, help="监听端口（默认 8081）")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()
    _config_path = args.config

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
