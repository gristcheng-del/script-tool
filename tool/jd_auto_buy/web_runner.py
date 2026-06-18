"""
京东自动购买 — Web 桥梁层
=========================
在 FastAPI 和 Playwright 脚本之间桥接日志、线程管理、配置 IO。

关键设计：
  1. 日志在导入任何业务模块之前就初始化好，避免 basicConfig 冲突
  2. Playwright（同步 API）在后台线程中运行，主线程只跑 asyncio
  3. queue.Queue + asyncio.run_coroutine_threadsafe() 桥接日志到 SSE
  4. 子类覆盖 JDAutoBuy / TimedRestock 中不适合 Web 环境的部分
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

# ═══════════════════════════════════════════════════════════
# 第一步：在所有 import 之前初始化日志
# 这确保 main.py / timed_restock.py 的 basicConfig() 是 no-op
# ═══════════════════════════════════════════════════════════

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_queue: queue.Queue = queue.Queue(maxsize=10000)

# 配置根日志器
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.handlers.clear()

# 文件输出
_fh = logging.FileHandler(
    LOG_DIR / f"webui_{datetime.now().strftime('%Y%m%d')}.log",
    encoding="utf-8",
)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
))
_root.addHandler(_fh)

# 控制台输出
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_root.addHandler(_ch)

# 队列输出（桥接到 SSE）
class _QueueHandler(logging.Handler):
    """将日志记录推送到线程安全队列，供 asyncio 消费"""
    def emit(self, record):
        try:
            log_queue.put({
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            })
        except Exception:
            pass  # 绝不因日志崩溃

_qh = _QueueHandler()
_qh.setFormatter(logging.Formatter("%(message)s"))
_root.addHandler(_qh)

logger = logging.getLogger("web_runner")

# ═══════════════════════════════════════════════════════════
# 第二步：安全地导入业务模块（basicConfig 已被抑制）
# ═══════════════════════════════════════════════════════════

# 先确保工作目录正确
_script_dir = Path(__file__).parent.resolve()
import os as _os
_os.chdir(str(_script_dir))

from main import JDAutoBuy
from timed_restock import TimedRestock, sync_server_time, parse_target_time

# ═══════════════════════════════════════════════════════════
# 日志捕获管理器
# ═══════════════════════════════════════════════════════════

class LogCaptureManager:
    """管理 queue.Queue → asyncio 的日志桥接，以及 SSE 客户端注册"""

    def __init__(self):
        self._sse_clients: dict[str, "asyncio.Queue"] = {}
        self._lock = threading.Lock()
        self._recent_logs: list[dict] = []  # 最近 500 条
        self._loop = None

    def setup(self, loop):
        """在 FastAPI startup 事件中调用，启动消费者线程"""
        self._loop = loop
        t = threading.Thread(target=self._consume_loop, daemon=True, name="log-consumer")
        t.start()
        logger.info("日志消费者线程已启动")

    def get_recent_logs(self, limit: int = 100, level: str = None) -> list:
        """返回最近 N 条日志"""
        logs = self._recent_logs
        if level and level.upper() != "ALL":
            logs = [l for l in logs if l["level"] == level.upper()]
        return logs[-limit:]

    def register_client(self, client_id: str) -> "asyncio.Queue":
        import asyncio
        q = asyncio.Queue(maxsize=500)
        with self._lock:
            self._sse_clients[client_id] = q
        return q

    def unregister_client(self, client_id: str):
        with self._lock:
            self._sse_clients.pop(client_id, None)

    def broadcast_status(self, status: dict):
        """广播状态变更给所有 SSE 客户端"""
        self._fanout({"type": "status", "data": status})

    def _fanout(self, msg: dict):
        if not self._loop:
            return
        text = json.dumps(msg, ensure_ascii=False)
        with self._lock:
            dead = []
            for cid, q in self._sse_clients.items():
                try:
                    import asyncio
                    asyncio.run_coroutine_threadsafe(q.put(text), self._loop)
                except Exception:
                    dead.append(cid)
            for cid in dead:
                self._sse_clients.pop(cid, None)

    def _consume_loop(self):
        """在守护线程中运行，从 log_queue 消费并扇出到 asyncio 队列"""
        while True:
            record = log_queue.get()
            try:
                self._recent_logs.append(record)
                if len(self._recent_logs) > 500:
                    self._recent_logs.pop(0)
                self._fanout({"type": "log", "data": record})
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════
# Web 适配子类
# ═══════════════════════════════════════════════════════════

class WebJDAutoBuy(JDAutoBuy):
    """
    JDAutoBuy 的 Web 适配版。
    - 去掉 signal 注册（线程中不可用）
    - run() 改为 Event 驱动的循环
    - run_once() 去掉 input() 阻塞
    - 增加 cleanup() 修复 Playwright 泄漏
    """

    def __init__(self, config: dict, dry_run: bool = False,
                 stop_event: threading.Event = None,
                 check_now_event: threading.Event = None):
        # 跳过父类 __init__ 中的 signal 注册
        self.config = config
        self.dry_run = dry_run
        self.user_data_dir = Path(config["browser"].get("user_data_dir", "./browser_data")).resolve()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.selectors = config.get("selectors", {})
        self.running = True
        self._stop_event = stop_event or threading.Event()
        self._check_now_event = check_now_event or threading.Event()
        self._playwright = None
        self._context = None
        self._last_check_time: Optional[datetime] = None

    def cleanup(self):
        """修复 Playwright 泄漏：关闭 context 并停止 playwright"""
        errors = []
        if hasattr(self, '_context') and self._context:
            try:
                self._context.close()
            except Exception as e:
                errors.append(f"context.close: {e}")
            self._context = None
        if hasattr(self, '_playwright') and self._playwright:
            try:
                self._playwright.stop()
            except Exception as e:
                errors.append(f"playwright.stop: {e}")
            self._playwright = None
        if errors:
            logger.warning(f"浏览器清理出错: {errors}")

    def launch_browser(self):
        """覆盖以保存 playwright 实例引用"""
        from stealth_utils import (
            get_browser_channel, get_common_viewport, get_browser_args, PAGE_INIT_SCRIPT
        )
        from playwright.sync_api import sync_playwright

        channel, channel_desc = get_browser_channel()
        if channel:
            logger.info(f"使用系统浏览器: {channel_desc}")
        else:
            logger.warning("未检测到系统 Chrome/Edge，使用 Playwright 自带 Chromium")

        p = sync_playwright().start()
        self._playwright = p
        viewport = get_common_viewport()

        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": False,
            "slow_mo": self.config["browser"].get("slow_mo", 300),
            "viewport": viewport,
            "locale": "zh-CN",
            "args": get_browser_args(),
        }
        if channel:
            launch_kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        context.add_init_script(PAGE_INIT_SCRIPT)
        self._context = context
        return context

    def run(self):
        """Event 驱动的监控循环（替代 signal + KeyboardInterrupt）"""
        from stealth_utils import IntervalRandomizer

        logger.info("=" * 50)
        logger.info("京东自动补货脚本启动（Web 模式）")
        logger.info("=" * 50)

        context = self.launch_browser()
        page = context.new_page()

        try:
            if not self.ensure_logged_in(page):
                logger.error("登录失败，退出")
                return
            page.close()

            check_interval = self.config["schedule"].get("check_interval_minutes", 10)
            active_hours = self.config["schedule"].get("active_hours", [8, 23])
            interval_rng = IntervalRandomizer(check_interval)

            logger.info(f"监控循环启动，基准间隔 {check_interval} 分钟")
            logger.info(f"活跃时段: {active_hours[0]}:00 - {active_hours[1]}:00")

            while self.running and not self._stop_event.is_set():
                now = datetime.now()
                current_hour = now.hour

                if active_hours[0] <= current_hour < active_hours[1] or self._check_now_event.is_set():
                    logger.info("-" * 40)
                    logger.info(f"轮次开始 - {now.strftime('%Y-%m-%d %H:%M:%S')}")
                    self.check_and_buy(context)
                    self._last_check_time = datetime.now()
                    self._check_now_event.clear()
                else:
                    logger.debug(f"当前 {current_hour}:00 不在活跃时段，等待...")

                sleep_sec = interval_rng.next_sleep_seconds()
                logger.info(f"等待 {sleep_sec / 60:.1f} 分钟...")
                for _ in range(sleep_sec):
                    if self._stop_event.is_set() or not self.running:
                        break
                    if self._check_now_event.is_set():
                        break  # 立即响应检查请求
                    time.sleep(1)

        except Exception as e:
            logger.error(f"监控循环异常: {e}", exc_info=True)
        finally:
            logger.info("监控循环结束，清理资源...")
            try:
                context.close()
            except Exception:
                pass
            self.cleanup()

    def run_once(self):
        """单次检查（无 input 阻塞）"""
        logger.info("=" * 50)
        logger.info("单次检查模式（Web）")
        logger.info("=" * 50)

        context = self.launch_browser()
        page = context.new_page()

        try:
            if not self.ensure_logged_in(page):
                logger.error("登录失败")
                return
            page.close()
            self.check_and_buy(context)
            self._last_check_time = datetime.now()
        except Exception as e:
            logger.error(f"检查异常: {e}", exc_info=True)
        finally:
            try:
                context.close()
            except Exception:
                pass
            self.cleanup()


class WebTimedRestock(TimedRestock):
    """
    TimedRestock 的 Web 适配版。
    - wait_until_fire_time() 支持取消
    - run() 结尾用 Event 替代 while True
    - 增加 cleanup()
    """

    def __init__(self, config: dict, target_time: tuple, advance: int,
                 dry_run: bool = False, cancel_event: threading.Event = None):
        # 跳过父类 __init__ 中的重复逻辑，直接设置
        self.config = config
        self.target_h, self.target_m, self.target_s = target_time
        self.advance_seconds = advance
        self.dry_run = dry_run
        self.user_data_dir = Path(config["browser"].get("user_data_dir", "./browser_data")).resolve()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.selectors = config.get("selectors", {})
        self.time_offset = 0.0
        self.purchase_attempted = False
        self.page_status = "unknown"
        self.pre_warmed = False
        self._cancel_event = cancel_event or threading.Event()
        self._playwright = None
        self._context = None

    def cleanup(self):
        errors = []
        if hasattr(self, '_context') and self._context:
            try:
                self._context.close()
            except Exception as e:
                errors.append(f"context.close: {e}")
            self._context = None
        if hasattr(self, '_playwright') and self._playwright:
            try:
                self._playwright.stop()
            except Exception as e:
                errors.append(f"playwright.stop: {e}")
            self._playwright = None
        if errors:
            logger.warning(f"浏览器清理出错: {errors}")

    def launch_browser(self):
        from stealth_utils import (
            get_browser_channel, get_common_viewport, get_browser_args, PAGE_INIT_SCRIPT
        )
        from playwright.sync_api import sync_playwright

        channel, channel_desc = get_browser_channel()
        if channel:
            logger.info(f"使用系统浏览器: {channel_desc}")
        else:
            logger.warning("未检测到系统 Chrome/Edge")

        p = sync_playwright().start()
        self._playwright = p
        viewport = get_common_viewport()

        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": False,
            "slow_mo": 0,
            "viewport": viewport,
            "locale": "zh-CN",
            "args": get_browser_args(),
        }
        if channel:
            launch_kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        context.add_init_script(PAGE_INIT_SCRIPT)
        self._context = context
        return context

    def wait_until_fire_time(self) -> bool:
        """覆盖以支持取消信号"""
        logger.info("=" * 50)
        logger.info(f"目标时间: {self.target_h:02d}:{self.target_m:02d}:{self.target_s:02d}")
        logger.info(f"当前服务器时间: {self._server_str()}")
        logger.info("=" * 50)

        while not self._cancel_event.is_set():
            from datetime import datetime, timezone, timedelta

            now = datetime.now(timezone.utc) + timedelta(seconds=self.time_offset)
            target = now.replace(
                hour=self.target_h, minute=self.target_m,
                second=self.target_s, microsecond=0,
            )
            remaining = (target - now).total_seconds()

            if remaining <= -5:
                logger.warning("目标时间已过 5 秒以上")
                return True

            if remaining <= 0.3:
                logger.info(f"倒计时: {remaining:.1f}s → 执行！")
                return True

            if remaining > 10:
                if int(remaining) % 10 == 0:
                    logger.info(f"距离目标还有 {int(remaining)} 秒...")
            elif remaining > 3:
                logger.info(f"距离目标还有 {remaining:.1f} 秒...")
            else:
                logger.info(f"!!! {remaining:.2f}s !!!")

            time.sleep(0.1)

        logger.info("收到取消信号")
        return False

    def _server_str(self) -> str:
        from datetime import datetime, timezone, timedelta
        t = datetime.now(timezone.utc) + timedelta(seconds=self.time_offset)
        return t.strftime("%H:%M:%S.%f")[:-3]

    def run(self):
        """覆盖主流程，支持取消和清理"""
        self.time_offset = sync_server_time()
        logger.info(f"时间偏差: {self.time_offset:+.3f}s")

        context = self.launch_browser()
        login_page = self.create_page(context)

        try:
            if not self.ensure_logged_in(login_page):
                logger.error("登录失败")
                return
            login_page.close()

            products = [p for p in self.config.get("products", []) if p.get("enabled", True)]
            if not products:
                logger.error("没有启用的商品")
                return

            product = products[0]
            logger.info(f"目标商品: {product['name']}")

            buy_page = self.create_page(context)
            self.pre_warm(buy_page, product)

            logger.info(f"预热完成，等待目标时间...")
            if not self.wait_until_fire_time():
                logger.info("已取消")
                return

            self.execute_buy(buy_page, product)

            # 保持浏览器打开直到用户取消
            logger.info("脚本保持运行，浏览器不会关闭。可在 Web UI 点击停止。")
            while not self._cancel_event.is_set():
                time.sleep(1)

        except Exception as e:
            logger.error(f"抢购异常: {e}", exc_info=True)
        finally:
            try:
                context.close()
            except Exception:
                pass
            self.cleanup()
            logger.info("已退出")


# ═══════════════════════════════════════════════════════════
# 任务管理器
# ═══════════════════════════════════════════════════════════

class MonitorRunner:
    """管理监控/抢购任务的生命周期，线程安全"""

    def __init__(self, config_path: str, log_capture: LogCaptureManager):
        self.config_path = Path(config_path)
        self.log_capture = log_capture
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._check_now_event: Optional[threading.Event] = None
        self._bot: Optional[WebJDAutoBuy] = None
        self._restock: Optional[WebTimedRestock] = None
        self._mode: str = "idle"
        self._start_time: Optional[datetime] = None
        self._error_message: Optional[str] = None
        self._next_product_id: int = 1

    # ── 状态 ──
    @property
    def status(self) -> dict:
        with self._lock:
            bot = self._bot
            last = bot._last_check_time if bot else None
            return {
                "status": self._mode,
                "monitoring": self._mode == "monitoring",
                "restock_running": self._mode == "restock_running",
                "uptime_seconds": (
                    (datetime.now() - self._start_time).total_seconds()
                    if self._start_time else 0
                ),
                "last_check_time": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
                "product_count": self._count_enabled_products(),
                "error_message": self._error_message,
            }

    # ── 配置 IO ──
    def load_config(self) -> dict:
        with self._lock:
            return self._load_config_unsafe()

    def _load_config_unsafe(self) -> dict:
        path = self.config_path
        if not path.exists():
            return {"products": [], "browser": {}, "schedule": {}, "checkout": {}, "selectors": {}}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self, config: dict) -> bool:
        with self._lock:
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                self._sync_product_ids(config)
                return True
            except Exception as e:
                logger.error(f"保存配置失败: {e}")
                self._error_message = str(e)
                return False

    def _sync_product_ids(self, config: dict):
        """同步产品 ID 计数器"""
        products = config.get("products", [])
        if products:
            max_id = max((p.get("id", 0) for p in products), default=0)
            self._next_product_id = max_id + 1

    def _count_enabled_products(self) -> int:
        try:
            config = self._load_config_unsafe()
            return sum(1 for p in config.get("products", []) if p.get("enabled", True))
        except Exception:
            return 0

    # ── 产品 CRUD ──
    def get_products(self) -> list:
        # 每次获取产品时先检查修复 ID（防止旧数据无 ID 导致前端异常）
        self._repair_product_ids()
        return self.load_config().get("products", [])

    def add_product(self, data: dict) -> int:
        with self._lock:
            config = self._load_config_unsafe()
            products = config.get("products", [])
            new_id = self._next_product_id
            self._next_product_id += 1
            product = {
                "id": new_id,
                "name": data.get("name", ""),
                "url": data.get("url", ""),
                "enabled": data.get("enabled", True),
                "size": data.get("size", ""),
                "color": data.get("color", ""),
                "max_price": data.get("max_price", 0),
                "quantity": data.get("quantity", 1),
            }
            products.append(product)
            config["products"] = products
            self._save_unsafe(config)
            return new_id

    def update_product(self, product_id: int, data: dict) -> bool:
        with self._lock:
            config = self._load_config_unsafe()
            products = config.get("products", [])
            for p in products:
                if p.get("id") == product_id:
                    for key in ("name", "url", "enabled", "size", "color", "max_price", "quantity"):
                        if key in data and data[key] is not None:
                            p[key] = data[key]
                    self._save_unsafe(config)
                    return True
            return False

    def delete_product(self, product_id: int) -> bool:
        with self._lock:
            # 先修复 ID（防止旧数据无 ID 导致误删全部）
            self._repair_product_ids_nolock()
            config = self._load_config_unsafe()
            products = config.get("products", [])
            # 找到要删除的产品（精确匹配，不依赖过滤）
            target = None
            for i, p in enumerate(products):
                if p.get("id") == product_id:
                    target = i
                    break
            if target is None:
                return False
            deleted_name = products[target].get("name", "未知")
            products.pop(target)
            config["products"] = products
            self._save_unsafe(config)
            self._sync_product_ids(config)
            logger.info(f"已删除产品: {deleted_name} (id={product_id})")
            return True

    def _repair_product_ids_nolock(self):
        """_repair_product_ids 的无锁版本（调用者需持有锁）"""
        # 直接内联避免死锁
        config = self._load_config_unsafe()
        products = config.get("products", [])
        if not products:
            return
        repaired = False
        seen_ids = set()
        for p in products:
            pid = p.get("id")
            if "id" not in p or pid is None:
                p["id"] = self._next_product_id
                self._next_product_id += 1
                repaired = True
            elif pid in seen_ids:
                p["id"] = self._next_product_id
                self._next_product_id += 1
                repaired = True
            else:
                seen_ids.add(pid)
        if repaired:
            self._save_unsafe(config)

    def _repair_product_ids(self):
        """给没有 id 或 id 重复的产品分配唯一 ID"""
        config = self._load_config_unsafe()
        products = config.get("products", [])
        if not products:
            return
        repaired = False
        seen_ids = set()
        for p in products:
            pid = p.get("id")
            # 情况1：无 id
            if "id" not in p or pid is None:
                p["id"] = self._next_product_id
                self._next_product_id += 1
                repaired = True
                logger.info(f"修复产品 ID: {p.get('name', '未知')} → id={p['id']}")
            # 情况2：id 重复
            elif pid in seen_ids:
                old_id = pid
                p["id"] = self._next_product_id
                self._next_product_id += 1
                repaired = True
                logger.warning(
                    f"修复重复 ID: {p.get('name', '未知')} id={old_id} → 新 id={p['id']}"
                )
            else:
                seen_ids.add(pid)
        if repaired:
            self._save_unsafe(config)

    def _save_unsafe(self, config: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)

    # ── 监控控制 ──
    def start_monitoring(self, dry_run: bool = False) -> bool:
        with self._lock:
            if self._mode != "idle":
                return False
            config = self._load_config_unsafe()
            products = [p for p in config.get("products", []) if p.get("enabled", True)]
            if not products:
                raise ValueError("没有启用的商品，请先在「产品」页面添加并启用")

            config_copy = copy.deepcopy(config)
            self._stop_event = threading.Event()
            self._check_now_event = threading.Event()
            self._mode = "monitoring"
            self._start_time = datetime.now()
            self._error_message = None

            self._thread = threading.Thread(
                target=self._run_monitor,
                args=(config_copy, dry_run),
                daemon=True,
                name="monitor-thread",
            )
            self._thread.start()
            self.log_capture.broadcast_status(self.status)
            return True

    def _run_monitor(self, config: dict, dry_run: bool):
        try:
            bot = WebJDAutoBuy(
                config, dry_run=dry_run,
                stop_event=self._stop_event,
                check_now_event=self._check_now_event,
            )
            with self._lock:
                self._bot = bot
            self.log_capture.broadcast_status(self.status)
            bot.run()
        except Exception as e:
            logger.error(f"监控线程异常: {e}", exc_info=True)
            with self._lock:
                self._error_message = str(e)
        finally:
            if self._bot:
                self._bot.cleanup()
            with self._lock:
                self._mode = "idle"
                self._bot = None
                self._thread = None
            self.log_capture.broadcast_status(self.status)

    def stop(self) -> bool:
        with self._lock:
            if self._mode == "idle":
                return False
            if self._stop_event:
                self._stop_event.set()
            if self._bot:
                self._bot.running = False
            if self._restock:
                self._restock._cancel_event.set()
            logger.info("正在停止任务...")
            return True

    def trigger_check_now(self) -> bool:
        with self._lock:
            if self._mode != "monitoring":
                return False
            if self._check_now_event:
                self._check_now_event.set()
            return True

    def run_check_once(self, dry_run: bool = False) -> bool:
        """在后台线程中执行单次检查（不启动循环监控）"""
        with self._lock:
            if self._mode != "idle":
                return False
            config = self._load_config_unsafe()
            products = [p for p in config.get("products", []) if p.get("enabled", True)]
            if not products:
                raise ValueError("没有启用的商品")

            config_copy = copy.deepcopy(config)
            self._mode = "checking"
            self._start_time = datetime.now()
            self._error_message = None

            self._thread = threading.Thread(
                target=self._run_check_once,
                args=(config_copy, dry_run),
                daemon=True,
                name="check-once-thread",
            )
            self._thread.start()
            self.log_capture.broadcast_status(self.status)
            return True

    def _run_check_once(self, config: dict, dry_run: bool):
        try:
            bot = WebJDAutoBuy(config, dry_run=dry_run)
            with self._lock:
                self._bot = bot
            bot.run_once()
        except Exception as e:
            logger.error(f"单次检查异常: {e}", exc_info=True)
        finally:
            if self._bot:
                self._bot.cleanup()
            with self._lock:
                self._mode = "idle"
                self._bot = None
                self._thread = None
            self.log_capture.broadcast_status(self.status)

    # ── 抢购控制 ──
    def start_restock(self, target_time: str, advance: int,
                      dry_run: bool = False, product_id: int = None) -> bool:
        with self._lock:
            if self._mode != "idle":
                return False
            config = self._load_config_unsafe()
            products = config.get("products", [])

            if product_id is not None:
                products = [p for p in products if p.get("id") == product_id]
            else:
                products = [p for p in products if p.get("enabled", True)]

            if not products:
                raise ValueError("没有可抢购的商品")

            config_copy = copy.deepcopy(config)
            # 只保留目标产品
            config_copy["products"] = [copy.deepcopy(products[0])]
            config_copy["products"][0]["enabled"] = True

            target = parse_target_time(target_time)
            advance = max(advance, 15)

            self._mode = "restock_running"
            self._start_time = datetime.now()
            self._error_message = None

            self._thread = threading.Thread(
                target=self._run_restock,
                args=(config_copy, target, advance, dry_run),
                daemon=True,
                name="restock-thread",
            )
            self._thread.start()
            self.log_capture.broadcast_status(self.status)
            return True

    def _run_restock(self, config: dict, target: tuple, advance: int, dry_run: bool):
        try:
            cancel_event = threading.Event()
            with self._lock:
                if self._stop_event:
                    cancel_event = self._stop_event
            restock = WebTimedRestock(
                config, target, advance,
                dry_run=dry_run,
                cancel_event=cancel_event,
            )
            with self._lock:
                self._restock = restock
            self.log_capture.broadcast_status(self.status)
            restock.run()
        except Exception as e:
            logger.error(f"抢购线程异常: {e}", exc_info=True)
            with self._lock:
                self._error_message = str(e)
        finally:
            if self._restock:
                self._restock.cleanup()
            with self._lock:
                self._mode = "idle"
                self._restock = None
                self._thread = None
            self.log_capture.broadcast_status(self.status)

    # ── 设置更新 ──
    def update_settings(self, sections: dict):
        with self._lock:
            config = self._load_config_unsafe()
            for section, values in sections.items():
                if values and section in ("schedule", "browser", "checkout", "selectors"):
                    if section not in config:
                        config[section] = {}
                    config[section].update(values)
            self._save_unsafe(config)
