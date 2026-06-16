"""
大麦网自动抢票 — 核心模块
========================
支持两种模式：
  1. 定时抢购：倒计时归零 → 选票档 → 下单
  2. 周期监控：定时检查是否有票（回流/补票）

大麦购买流程：
  商品页 → 倒计时归零 →「立即购买」→ 选票档弹层 → 确认 → 订单页 → 提交

注意：
  - 选座场景（Canvas 座位图）复杂度极高且不稳定，暂不支持
  - 本工具仅支持选票档模式（大部分演唱会适用）
  - 大麦风控比京东更严，请合理使用
"""

import json
import logging
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

logger = logging.getLogger("damai")

# ---------------------------------------------------------------------------
# 时间同步
# ---------------------------------------------------------------------------
def sync_server_time(host: str = "www.damai.cn") -> float:
    """同步大麦服务器时间，返回偏差秒数（server - local）"""
    logger.info(f"正在同步 {host} 服务器时间...")
    offsets = []
    for i in range(3):
        try:
            t0 = datetime.now(timezone.utc)
            req = urllib.request.Request(
                f"https://{host}/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    ),
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                server_date = resp.headers.get("Date", "")
            t1 = datetime.now(timezone.utc)

            server_time = datetime.strptime(server_date, "%a, %d %b %Y %H:%M:%S %Z")
            server_time = server_time.replace(tzinfo=timezone.utc)

            rtt = (t1 - t0).total_seconds()
            local_mid = t0 + timedelta(seconds=rtt / 2)
            offset = (server_time - local_mid).total_seconds()
            offsets.append(offset)
            logger.info(f"  第{i+1}次: 偏差={offset:+.3f}s, RTT={rtt*1000:.0f}ms")
        except Exception as e:
            logger.warning(f"  第{i+1}次失败: {e}")

    if not offsets:
        logger.error("时间同步失败，使用本地时间")
        return 0.0

    avg = sum(offsets) / len(offsets)
    logger.info(f"时间同步完成，偏差: {avg:+.3f}s")
    return avg


def now_server(offset: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        logger.error(f"配置文件不存在: {p}")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_target_time(time_str: str) -> Tuple[int, int, int]:
    """解析 'HH:MM' 或 'HH:MM:SS'"""
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        return int(parts[0]), int(parts[1]), int(parts[2])
    raise ValueError(f"时间格式错误: {time_str}")


# ---------------------------------------------------------------------------
# 大麦抢票核心类
# ---------------------------------------------------------------------------
class DamaiBuyer:
    """大麦网自动抢票"""

    # ── 大麦页面选择器（可能需要根据实际页面调整） ──
    SELECTORS = {
        # 登录状态
        "logged_in": ".user-avatar, .login-user, .my-user, .header-user",
        "not_logged_in": "a:has-text('登录'), .login-btn, .no-login",
        # 倒计时
        "countdown_container": ".countdown, .time-left, [data-countdown], .count-down",
        # 购买按钮
        "buy_btn_disabled": ".buy-btn.disabled, .btn-disable, a:has-text('即将开抢')",
        "buy_btn_active": [
            ".buy-btn:not(.disabled)",
            "#buyReady",
            "a:has-text('立即购买')",
            "button:has-text('立即购买')",
            ".btn-buy",
        ],
        # 票档选择弹层
        "sku_modal": ".sku-modal, .ticket-modal, .perform-sku, .buy-tips",
        "sku_items": ".sku-item, .ticket-item, .perform-sku-item, li[data-sku]",
        "sku_price": ".price, .sku-price, .ticket-price",
        "sku_selected": ".selected, .active, .cur",
        "sku_confirm": ".btn-ok, .confirm-btn, a:has-text('确定'), button:has-text('确定')",
        "qty_select": ".qty-select, .buy-num, .count-select",
        "qty_increase": ".qty-plus, .btn-plus, .num-plus",
        # 订单确认页
        "order_submit": "#submitOrder, .submit-btn, button:has-text('提交订单'), a:has-text('提交订单')",
        "order_page_url": ["buy.damai.cn", "order.damai.cn", "confirm"],
        # 无票标记
        "sold_out": "text=缺货登记, text=已售罄, text=暂无票, .sold-out",
    }

    def __init__(self, config: dict, target_time: tuple = None,
                 advance: int = 30, dry_run: bool = False):
        self.config = config
        self.target_h, self.target_m, self.target_s = target_time or (0, 0, 0)
        self.advance_seconds = max(advance, 15)
        self.dry_run = dry_run
        self.user_data_dir = Path(
            config.get("browser", {}).get("user_data_dir", "./browser_data")
        ).resolve()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.time_offset = 0.0
        self._playwright = None
        self._context = None

    # ── 浏览器 ──
    def launch_browser(self) -> BrowserContext:
        """启动浏览器（优先系统 Chrome）"""
        # 检测系统浏览器
        channel = None
        try:
            import winreg
            for name, reg_path in [
                ("chrome", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                ("msedge", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            ]:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                    if Path(winreg.QueryValue(key, None)).exists():
                        channel = name
                        logger.info(f"使用系统浏览器: {name}")
                    winreg.CloseKey(key)
                    if channel:
                        break
                except Exception:
                    continue
        except Exception:
            pass

        viewport = random.choice([
            {"width": 1920, "height": 1080},
            {"width": 1440, "height": 900},
            {"width": 1536, "height": 864},
        ])

        p = sync_playwright().start()
        self._playwright = p

        kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": False,
            "slow_mo": 0,  # 抢购时零延迟
            "viewport": viewport,
            "locale": "zh-CN",
            "args": ["--lang=zh-CN", "--no-sandbox"],
        }
        if channel:
            kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**kwargs)

        # 基础反检测注入
        context.add_init_script("""
(function() {
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
    try {
        if (!navigator.languages || navigator.languages.length === 0) {
            Object.defineProperty(navigator, 'languages', {
                get: function() { return ['zh-CN', 'zh', 'en']; },
                enumerable: true, configurable: true,
            });
        }
    } catch(e) {}
})();
""")
        self._context = context
        return context

    def cleanup(self):
        """清理浏览器资源"""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ── 登录 ──
    def ensure_logged_in(self, page: Page) -> bool:
        """确认已登录大麦（淘宝/支付宝扫码）"""
        page.goto("https://www.damai.cn/", wait_until="domcontentloaded")
        time.sleep(2)

        sel = self.SELECTORS
        # 检查登录状态
        try:
            for s in sel["logged_in"].split(", "):
                el = page.locator(s).first
                if el.count() > 0 and el.is_visible():
                    logger.info("检测到已登录状态")
                    return True
        except Exception:
            pass

        # 未登录，引导用户登录
        logger.info("=" * 50)
        logger.info("未检测到登录状态，即将打开大麦首页")
        logger.info("请在浏览器中点击右上角「登录」扫码（淘宝/支付宝）")
        logger.info("等待 120 秒...")
        logger.info("=" * 50)

        try:
            # 等页面 URL 变化或用户元素出现
            page.wait_for_selector(sel["logged_in"], timeout=120_000)
            logger.info("登录成功！状态已保存，下次无需重复登录。")
            return True
        except PlaywrightTimeout:
            logger.error("登录超时，请重试")
            return False

    # ── 预热 ──
    def pre_warm(self, page: Page, event: dict):
        """预热：访问演出页面，模拟自然浏览，预选票档"""
        name = event.get("name", "未知演出")
        url = event.get("url", "")

        logger.info(f"[{name}] === 预热阶段 ===")

        # 1. 先访问大麦首页（建立自然的 referrer 链）
        logger.info(f"[{name}] 模拟自然浏览路径...")
        page.goto("https://www.damai.cn/", wait_until="domcontentloaded")
        self._human_delay(1.5, 3.0)
        # 模拟滚动浏览
        page.evaluate(f"window.scrollBy(0, {random.randint(300, 600)})")
        self._human_delay(0.8, 2.0)

        # 2. 访问目标演出页
        logger.info(f"[{name}] 加载演出页面: {url}")
        page.goto(url, wait_until="domcontentloaded", referer="https://www.damai.cn/")
        self._human_delay(2.0, 3.5)

        # 3. 模拟浏览演出详情
        page.evaluate(f"window.scrollBy(0, {random.randint(400, 800)})")
        self._human_delay(1.0, 2.0)
        page.evaluate(f"window.scrollBy(0, {random.randint(200, 500)})")
        self._human_delay(0.5, 1.5)

        # 4. 检测页面状态
        status = self._detect_page_status(page, event)
        logger.info(f"[{name}] 页面状态: {status}")
        self._page_status = status

    def _detect_page_status(self, page: Page, event: dict) -> str:
        """检测演出页面当前状态"""
        body = page.locator("body").inner_text()

        # 倒计时场景
        if any(kw in body for kw in ["即将开抢", "倒计时", "距离开始", "开售倒计时", "countdown"]):
            logger.info("  检测到倒计时 → 使用「等待按钮变化」策略")
            return "countdown"

        # 已开售但在售
        if any(kw in body for kw in ["立即购买", "选座购买", "立即预定"]):
            logger.info("  检测到购买按钮 → 当前可直接购买")
            return "on_sale"

        # 已售罄
        if any(kw in body for kw in ["缺货登记", "已售罄", "暂无票", "已结束"]):
            logger.info("  检测到售罄标记")
            return "sold_out"

        return "unknown"

    # ── 辅助 ──
    def _human_delay(self, min_s: float = 0.3, max_s: float = 2.0):
        """Gamma 分布的人类化延迟"""
        mean = (min_s + max_s) / 2
        delay = random.gammavariate(alpha=2.0, beta=mean / 2)
        delay = max(min_s, min(delay, max_s * 1.5))
        time.sleep(delay)

    # ── 等待目标时间 ──
    def wait_until_fire_time(self, cancel_check=None) -> bool:
        """等待目标时间前 0.3 秒"""
        logger.info("=" * 50)
        logger.info(f"目标时间: {self.target_h:02d}:{self.target_m:02d}:{self.target_s:02d}")
        logger.info(f"当前服务器时间: {self._server_str()}")
        logger.info("=" * 50)

        while True:
            if cancel_check and cancel_check():
                logger.info("收到取消信号")
                return False

            now = now_server(self.time_offset)
            target = now.replace(
                hour=self.target_h, minute=self.target_m,
                second=self.target_s, microsecond=0,
            )
            remaining = (target - now).total_seconds()

            if remaining <= -5:
                logger.warning("目标时间已过 5 秒")
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

    def _server_str(self) -> str:
        t = now_server(self.time_offset)
        return t.strftime("%H:%M:%S.%f")[:-3]

    # ── 执行抢购 ──
    def execute_buy(self, page: Page, event: dict):
        """执行抢购：点击购买 → 选票档 → 确认 → 订单页"""
        name = event["name"]
        logger.info(f"[{name}] === 执行抢购 ===")
        t0 = time.perf_counter()

        if self._page_status in ("countdown",):
            self._buy_countdown_strategy(page, event)
        elif self._page_status == "on_sale":
            self._buy_direct(page, event)
        else:
            # 未知状态或售罄，尝试刷新
            self._buy_refresh_strategy(page, event)

        elapsed = time.perf_counter() - t0
        logger.info(f"[{name}] 抢购动作耗时: {elapsed:.2f}s")

    def _buy_countdown_strategy(self, page: Page, event: dict):
        """倒计时策略：等按钮自动变为可购买状态"""
        name = event["name"]
        logger.info(f"[{name}] 策略：等待倒计时按钮自动变化")

        buy_selectors = [
            ".buy-btn:not(.disabled)",
            "#buyReady",
            "a:has-text('立即购买')",
            "button:has-text('立即购买')",
            "a:has-text('选座购买')",
            "button:has-text('选座购买')",
            "a:has-text('立即预定')",
        ]

        clicked = False
        deadline = time.time() + 8

        while time.time() < deadline:
            for sel in buy_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        cls = (btn.get_attribute("class") or "").lower()
                        if "disable" not in cls and "disabled" not in cls:
                            logger.info(f"[{name}] 点击: {sel}")
                            btn.click()
                            clicked = True
                            break
                except Exception:
                    continue
            if clicked:
                break
            time.sleep(0.05)

        if not clicked:
            logger.warning(f"[{name}] 按钮未变化，尝试刷新页面...")
            self._buy_refresh_strategy(page, event)
            return

        self._after_buy_click(page, event)

    def _buy_direct(self, page: Page, event: dict):
        """直接购买（页面已经显示购买按钮）"""
        name = event["name"]
        logger.info(f"[{name}] 直接点击购买按钮")

        buy_selectors = [
            "a:has-text('立即购买')",
            "button:has-text('立即购买')",
            "#buyReady",
            ".buy-btn",
        ]
        clicked = False
        for sel in buy_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.error(f"[{name}] 未找到购买按钮")
            return

        self._after_buy_click(page, event)

    def _buy_refresh_strategy(self, page: Page, event: dict):
        """刷新策略：刷新页面后抢按钮"""
        name = event["name"]
        logger.info(f"[{name}] 刷新页面后抢按钮")

        page.reload(wait_until="commit")
        logger.info(f"[{name}] 已刷新")

        buy_selectors = [
            "a:has-text('立即购买')",
            "button:has-text('立即购买')",
            "#buyReady",
            ".buy-btn:not(.disabled)",
        ]

        clicked = False
        deadline = time.time() + 8

        while time.time() < deadline:
            for sel in buy_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        cls = (btn.get_attribute("class") or "").lower()
                        if "disable" not in cls and "disabled" not in cls:
                            btn.click()
                            clicked = True
                            break
                except Exception:
                    continue
            if clicked:
                break
            time.sleep(0.05)

        if not clicked:
            logger.error(f"[{name}] 未找到购买按钮，可能已售罄")
            return

        self._after_buy_click(page, event)

    def _after_buy_click(self, page: Page, event: dict):
        """点击购买按钮后：处理票档选择弹层 → 确认 → 进入订单页"""
        name = event["name"]
        tier = event.get("tier", "")
        quantity = event.get("quantity", 1)

        if self.dry_run:
            logger.info(f"[{name}] [DRY-RUN] 模拟到此结束")
            return

        # 等待弹层/页面变化
        time.sleep(0.5)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeout:
            pass

        logger.info(f"[{name}] 当前 URL: {page.url}")

        # 检测是否出现了票档选择弹层
        has_sku_modal = self._wait_for_sku_modal(page)
        if has_sku_modal:
            logger.info(f"[{name}] 检测到票档选择弹层")
            if not self._select_ticket_tier(page, event):
                logger.error(f"[{name}] 票档选择失败")
                return

        # 检测是否在订单确认页
        if any(kw in page.url for kw in ["buy.damai.cn", "order", "confirm", "trade"]):
            logger.info(f"[{name}] 已进入订单确认页")
            self._try_submit_order(page, event)
            return

        # 未进入订单页，可能还需要处理中间页
        logger.info(f"[{name}] 等待进入订单页...")
        try:
            page.wait_for_url("**/buy.damai.cn/**", timeout=15000)
            logger.info(f"[{name}] 已进入订单页")
            self._try_submit_order(page, event)
        except PlaywrightTimeout:
            logger.warning(f"[{name}] 未能在 15 秒内进入订单页，当前 URL: {page.url}")

    def _wait_for_sku_modal(self, page: Page) -> bool:
        """检测票档选择弹层是否出现"""
        sel = self.SELECTORS
        indicators = [
            sel["sku_modal"],
            ".sku-modal",
            ".perform-sku",
            ".buy-tips",
            ".ticket-sku",
        ]
        for s in indicators:
            try:
                el = page.locator(s).first
                if el.count() > 0 and el.is_visible():
                    return True
            except Exception:
                continue

        # 备选：检查是否有票档相关文本
        try:
            body = page.locator("body").inner_text(timeout=2000)
            if any(kw in body for kw in ["票档", "价格", "数量", "请选择"]):
                return True
        except Exception:
            pass
        return False

    def _select_ticket_tier(self, page: Page, event: dict) -> bool:
        """在票档弹层中选择指定的价格档位和数量"""
        name = event["name"]
        tier = event.get("tier", "")
        quantity = event.get("quantity", 1)

        self._human_delay(0.3, 0.8)

        # 如果有指定票档，尝试匹配
        if tier:
            logger.info(f"[{name}] 选择票档: ¥{tier}")
            tier_selectors = [
                f"text={tier}",
                f"[data-price='{tier}']",
                f"li:has-text('{tier}')",
                f".sku-item:has-text('{tier}')",
                f".ticket-item:has-text('{tier}')",
            ]
            clicked = False
            for sel in tier_selectors:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible():
                        if "disabled" not in (el.get_attribute("class") or "").lower():
                            el.click()
                            self._human_delay(0.3, 0.6)
                            clicked = True
                            break
                except Exception:
                    continue

            if not clicked:
                logger.warning(f"[{name}] 未找到票档 ¥{tier} 或该票档不可选")

        # 调整数量
        if quantity > 1:
            self._adjust_quantity(page, quantity)

        self._human_delay(0.2, 0.5)

        # 点击确定
        confirm_selectors = [
            "a:has-text('确定')",
            "button:has-text('确定')",
            ".btn-ok",
            ".confirm-btn",
            "#confirmBtn",
        ]
        for sel in confirm_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    logger.info(f"[{name}] 已点击确定")
                    return True
            except Exception:
                continue

        logger.warning(f"[{name}] 未找到确定按钮")
        return False

    def _adjust_quantity(self, page: Page, quantity: int):
        """调整购买数量"""
        try:
            # 先尝试直接输入
            qty_input = page.locator("input[type='number'], .qty-input, .buy-num input").first
            if qty_input.count() > 0:
                qty_input.fill(str(quantity))
                return
        except Exception:
            pass

        # 备选：点加号
        for _ in range(quantity - 1):
            try:
                plus = page.locator(".qty-plus, .btn-plus, .num-plus, .increase").first
                if plus.count() > 0 and plus.is_visible():
                    plus.click()
                    time.sleep(0.1)
            except Exception:
                break

    def _try_submit_order(self, page: Page, event: dict):
        """在订单确认页提交订单"""
        name = event["name"]
        logger.info(f"[{name}] 订单确认页")

        self._human_delay(0.3, 0.8)

        auto_submit = self.config.get("checkout", {}).get("auto_submit_order", False)

        submit_selectors = [
            "#submitOrder",
            ".submit-btn",
            "button:has-text('提交订单')",
            "a:has-text('提交订单')",
            "button:has-text('同意以上协议并提交')",
        ]

        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    if auto_submit or self.dry_run:
                        btn.click()
                        logger.info(f"[{name}] 已点击提交订单")
                    logger.info("=" * 50)
                    logger.info(f"[{name}] 请在浏览器中完成后续支付操作")
                    logger.info("=" * 50)
                    return
            except Exception:
                continue

        logger.info(f"[{name}] 已到达下单页面，请手动完成")

    # ── 主流程 ──
    def run_timed(self, event: dict, cancel_check=None):
        """定时抢购主流程"""
        # 1. 时间同步
        self.time_offset = sync_server_time()
        logger.info(f"时间偏差: {self.time_offset:+.3f}s")

        # 2. 启动浏览器
        context = self.launch_browser()
        login_page = context.new_page()

        try:
            # 3. 登录
            if not self.ensure_logged_in(login_page):
                logger.error("登录失败")
                return
            login_page.close()

            # 4. 预热
            buy_page = context.new_page()
            self.pre_warm(buy_page, event)

            # 5. 等待目标时间
            if not self.wait_until_fire_time(cancel_check):
                logger.info("已取消")
                return

            # 6. 执行抢购
            self.execute_buy(buy_page, event)

            # 7. 保持浏览器打开
            logger.info("浏览器保持打开，请在浏览器中操作。按 Ctrl+C 退出。")
            while not (cancel_check and cancel_check()):
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("用户中断")
        except Exception as e:
            logger.error(f"异常: {e}", exc_info=True)
        finally:
            try:
                context.close()
            except Exception:
                pass
            self.cleanup()

    def run_monitor_once(self, event: dict) -> bool:
        """单次检查：访问页面，检查是否有票，有则尝试购买"""
        name = event["name"]
        logger.info(f"[{name}] 单次检查...")

        context = self.launch_browser()
        page = context.new_page()

        try:
            if not self.ensure_logged_in(page):
                logger.error("登录失败")
                return False
            page.close()

            buy_page = context.new_page()
            url = event["url"]
            buy_page.goto(url, wait_until="domcontentloaded")
            self._human_delay(1.5, 3.0)

            status = self._detect_page_status(buy_page, event)
            logger.info(f"[{name}] 状态: {status}")

            if status in ("on_sale", "countdown"):
                logger.info(f"[{name}] 有票！开始购买...")
                self.execute_buy(buy_page, event)
                return True
            else:
                logger.info(f"[{name}] 当前无票")
                return False

        except Exception as e:
            logger.error(f"[{name}] 检查异常: {e}")
            return False
        finally:
            try:
                context.close()
            except Exception:
                pass
            self.cleanup()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="大麦网自动抢票")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件")
    parser.add_argument("--time", "-t", help="目标时间 HH:MM 或 HH:MM:SS")
    parser.add_argument("--advance", "-a", type=int, default=30, help="预热提前秒数")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    parser.add_argument("--once", action="store_true", help="单次检查")
    args = parser.parse_args()

    # 设置日志
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"damai_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    os_module = __import__("os")
    os_module.chdir(Path(__file__).parent.resolve())

    config = load_config(args.config)
    events = [e for e in config.get("events", []) if e.get("enabled", True)]
    if not events:
        logger.error("没有启用的演出")
        return
    event = events[0]

    buyer = DamaiBuyer(config, dry_run=args.dry_run)

    if args.once:
        buyer.run_monitor_once(event)
    elif args.time:
        target = parse_target_time(args.time)
        buyer.target_h, buyer.target_m, buyer.target_s = target
        buyer.advance_seconds = max(args.advance, 15)
        buyer.run_timed(event)
    else:
        logger.error("请指定 --time 或 --once")
        sys.exit(1)


if __name__ == "__main__":
    main()
