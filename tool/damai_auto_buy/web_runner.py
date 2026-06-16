"""
大麦网自动抢票 — Web 桥梁层
===========================
在 FastAPI 和 DamaiBuyer 之间桥接日志、线程管理、配置 IO。
架构和 jd_auto_buy/web_runner.py 相同，适配大麦业务。
"""

import copy
import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════
# 第一步：日志初始化（在任何业务模块 import 之前）
# ═══════════════════════════════════════════════════════

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_queue: queue.Queue = queue.Queue(maxsize=10000)

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.handlers.clear()

_fh = logging.FileHandler(
    LOG_DIR / f"damai_webui_{datetime.now().strftime('%Y%m%d')}.log",
    encoding="utf-8",
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s"))
_root.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_root.addHandler(_ch)

class _QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            log_queue.put({
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            })
        except Exception:
            pass

_qh = _QueueHandler()
_qh.setFormatter(logging.Formatter("%(message)s"))
_root.addHandler(_qh)

logger = logging.getLogger("damai_web_runner")

# 设置工作目录
_script_dir = Path(__file__).parent.resolve()
import os as _os
_os.chdir(str(_script_dir))

# ═══════════════════════════════════════════════════════
# 第二步：导入业务模块
# ═══════════════════════════════════════════════════════

from damai_buy import DamaiBuyer, sync_server_time, parse_target_time


# ═══════════════════════════════════════════════════════
# 日志捕获管理器（同 jd_auto_buy 架构）
# ═══════════════════════════════════════════════════════

class LogCaptureManager:
    def __init__(self):
        self._sse_clients: dict = {}
        self._lock = threading.Lock()
        self._recent_logs: list = []
        self._loop = None

    def setup(self, loop):
        self._loop = loop
        t = threading.Thread(target=self._consume_loop, daemon=True, name="damai-log-consumer")
        t.start()
        logger.info("日志消费者线程已启动")

    def get_recent_logs(self, limit: int = 100, level: str = None) -> list:
        logs = self._recent_logs
        if level and level.upper() != "ALL":
            logs = [l for l in logs if l["level"] == level.upper()]
        return logs[-limit:]

    def register_client(self, client_id: str):
        import asyncio
        q = asyncio.Queue(maxsize=500)
        with self._lock:
            self._sse_clients[client_id] = q
        return q

    def unregister_client(self, client_id: str):
        with self._lock:
            self._sse_clients.pop(client_id, None)

    def broadcast_status(self, status: dict):
        self._fanout({"type": "status", "data": status})

    def _fanout(self, msg: dict):
        if not self._loop:
            return
        text = json.dumps(msg, ensure_ascii=False)
        with self._lock:
            dead = []
            for cid, q in list(self._sse_clients.items()):
                try:
                    import asyncio
                    asyncio.run_coroutine_threadsafe(q.put(text), self._loop)
                except Exception:
                    dead.append(cid)
            for cid in dead:
                self._sse_clients.pop(cid, None)

    def _consume_loop(self):
        while True:
            record = log_queue.get()
            try:
                self._recent_logs.append(record)
                if len(self._recent_logs) > 500:
                    self._recent_logs.pop(0)
                self._fanout({"type": "log", "data": record})
            except Exception:
                pass


# ═══════════════════════════════════════════════════════
# Web 适配的 DamaiBuyer
# ═══════════════════════════════════════════════════════

class WebDamaiBuyer(DamaiBuyer):
    """DamaiBuyer 的 Web 适配版，支持取消信号"""

    def __init__(self, config: dict, target_time: tuple = None,
                 advance: int = 30, dry_run: bool = False,
                 cancel_event: threading.Event = None):
        super().__init__(config, target_time, advance, dry_run)
        self._cancel_event = cancel_event or threading.Event()

    def wait_until_fire_time(self, cancel_check=None) -> bool:
        return super().wait_until_fire_time(
            cancel_check=lambda: self._cancel_event.is_set()
        )

    def run_timed(self, event: dict, cancel_check=None):
        super().run_timed(event, cancel_check=lambda: self._cancel_event.is_set())


# ═══════════════════════════════════════════════════════
# 任务管理器
# ═══════════════════════════════════════════════════════

class DamaiRunner:
    """管理大麦抢票任务的生命周期"""

    def __init__(self, config_path: str, log_capture: LogCaptureManager):
        self.config_path = Path(config_path)
        self.log_capture = log_capture
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None
        self._buyer: Optional[WebDamaiBuyer] = None
        self._mode: str = "idle"
        self._start_time: Optional[datetime] = None
        self._error_message: Optional[str] = None
        self._next_event_id: int = 1

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._mode,
                "running": self._mode in ("buying", "monitoring"),
                "uptime_seconds": (
                    (datetime.now() - self._start_time).total_seconds()
                    if self._start_time else 0
                ),
                "event_count": self._count_enabled_events(),
                "error_message": self._error_message,
            }

    # ── 配置 IO ──
    def load_config(self) -> dict:
        path = self.config_path
        if not path.exists():
            return {"events": [], "browser": {}, "schedule": {}, "checkout": {}, "selectors": {}}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self, config: dict) -> bool:
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            self._sync_event_ids(config)
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False

    def _sync_event_ids(self, config: dict):
        events = config.get("events", [])
        if events:
            max_id = max((e.get("id", 0) for e in events), default=0)
            self._next_event_id = max_id + 1

    def _count_enabled_events(self) -> int:
        try:
            config = self.load_config()
            return sum(1 for e in config.get("events", []) if e.get("enabled", True))
        except Exception:
            return 0

    # ── 演出 CRUD ──
    def get_events(self) -> list:
        return self.load_config().get("events", [])

    def add_event(self, data: dict) -> int:
        with self._lock:
            config = self.load_config()
            events = config.get("events", [])
            new_id = self._next_event_id
            self._next_event_id += 1
            event = {
                "id": new_id,
                "name": data.get("name", ""),
                "url": data.get("url", ""),
                "enabled": data.get("enabled", True),
                "tier": data.get("tier", ""),
                "quantity": data.get("quantity", 1),
                "max_price": data.get("max_price", 0),
            }
            events.append(event)
            config["events"] = events
            self._save_unsafe(config)
            return new_id

    def update_event(self, event_id: int, data: dict) -> bool:
        with self._lock:
            config = self.load_config()
            for e in config.get("events", []):
                if e.get("id") == event_id:
                    for key in ("name", "url", "enabled", "tier", "quantity", "max_price"):
                        if key in data and data[key] is not None:
                            e[key] = data[key]
                    self._save_unsafe(config)
                    return True
            return False

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            config = self.load_config()
            events = config.get("events", [])
            new_events = [e for e in events if e.get("id") != event_id]
            if len(new_events) == len(events):
                return False
            config["events"] = new_events
            self._save_unsafe(config)
            return True

    def _save_unsafe(self, config: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)

    # ── 抢购控制 ──
    def start_buy(self, target_time: str, advance: int,
                  dry_run: bool = False, event_id: int = None) -> bool:
        with self._lock:
            if self._mode != "idle":
                return False
            config = self.load_config()
            events = config.get("events", [])

            if event_id is not None:
                events = [e for e in events if e.get("id") == event_id]
            else:
                events = [e for e in events if e.get("enabled", True)]

            if not events:
                raise ValueError("没有可抢购的演出")

            config_copy = copy.deepcopy(config)
            config_copy["events"] = [copy.deepcopy(events[0])]
            config_copy["events"][0]["enabled"] = True

            target = parse_target_time(target_time)
            advance = max(advance, 15)

            self._cancel_event = threading.Event()
            self._mode = "buying"
            self._start_time = datetime.now()
            self._error_message = None

            self._thread = threading.Thread(
                target=self._run_buy,
                args=(config_copy, target, advance, dry_run),
                daemon=True,
                name="damai-buy-thread",
            )
            self._thread.start()
            self.log_capture.broadcast_status(self.status)
            return True

    def _run_buy(self, config: dict, target: tuple, advance: int, dry_run: bool):
        try:
            buyer = WebDamaiBuyer(
                config, target, advance, dry_run,
                cancel_event=self._cancel_event,
            )
            with self._lock:
                self._buyer = buyer
            self.log_capture.broadcast_status(self.status)
            events = config.get("events", [])
            if events:
                buyer.run_timed(events[0])
        except Exception as e:
            logger.error(f"抢购异常: {e}", exc_info=True)
            with self._lock:
                self._error_message = str(e)
        finally:
            if self._buyer:
                self._buyer.cleanup()
            with self._lock:
                self._mode = "idle"
                self._buyer = None
                self._thread = None
            self.log_capture.broadcast_status(self.status)

    def run_check_once(self, dry_run: bool = False) -> bool:
        """单次检查"""
        with self._lock:
            if self._mode != "idle":
                return False
            config = self.load_config()
            events = [e for e in config.get("events", []) if e.get("enabled", True)]
            if not events:
                raise ValueError("没有启用的演出")

            self._mode = "checking"
            self._start_time = datetime.now()
            self._error_message = None

            self._thread = threading.Thread(
                target=self._run_check_once,
                args=(copy.deepcopy(config), copy.deepcopy(events[0]), dry_run),
                daemon=True,
                name="damai-check-thread",
            )
            self._thread.start()
            self.log_capture.broadcast_status(self.status)
            return True

    def _run_check_once(self, config: dict, event: dict, dry_run: bool):
        try:
            buyer = DamaiBuyer(config, dry_run=dry_run)
            buyer.run_monitor_once(event)
        except Exception as e:
            logger.error(f"检查异常: {e}", exc_info=True)
        finally:
            buyer.cleanup()
            with self._lock:
                self._mode = "idle"
                self._thread = None
            self.log_capture.broadcast_status(self.status)

    def stop(self) -> bool:
        with self._lock:
            if self._mode == "idle":
                return False
            if self._cancel_event:
                self._cancel_event.set()
            logger.info("正在停止任务...")
            return True

    def update_settings(self, sections: dict):
        with self._lock:
            config = self.load_config()
            for section, values in sections.items():
                if values and section in ("schedule", "browser", "checkout", "selectors"):
                    if section not in config:
                        config[section] = {}
                    config[section].update(values)
            self._save_unsafe(config)
