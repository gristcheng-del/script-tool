"""
京东定时补货抢购脚本
====================
专门针对「已知补货时间点」的抢购场景，尽可能提升下单速度。

核心策略：
  1. 同步京东服务器时间（消除本地时钟偏差）
  2. 提前预热：产品页加载好、SKU 选好
  3. 时间到达前 0.5 秒刷新页面
  4. 按钮出现瞬间点击
  5. 跳过购物车，直通结算页

用法：
  python timed_restock.py --time 08:00:00
  python timed_restock.py --time 10:00:00 --advance 60
  python timed_restock.py --time 20:00:00 --dry-run

与 main.py 的区别：
  - main.py：周期性监控，适合「不知道什么时候补货」
  - timed_restock.py：定点抢购，适合「知道几点补货」
"""

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

# 反检测工具
from stealth_utils import (
    get_browser_channel,
    get_common_viewport,
    get_browser_args,
    PAGE_INIT_SCRIPT,
    human_delay,
    human_scroll,
    warm_browsing_session,
    order_page_ritual,
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"timed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("timed_restock")


# ---------------------------------------------------------------------------
# 时钟同步
# ---------------------------------------------------------------------------
def sync_server_time() -> float:
    """
    通过 JD 首页 HTTP 响应的 Date 头同步服务器时间。
    返回：本地时间与服务器时间的偏移秒数（server - local），正数表示服务器快。
    """
    logger.info("正在同步京东服务器时间...")
    offsets = []
    for i in range(3):
        try:
            t0 = datetime.now(timezone.utc)
            req = urllib.request.Request(
                "https://www.jd.com/",
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

            # 解析 Date 头: "Tue, 16 Jun 2026 00:00:00 GMT"
            server_time = datetime.strptime(server_date, "%a, %d %b %Y %H:%M:%S %Z")
            server_time = server_time.replace(tzinfo=timezone.utc)

            # 用发送和接收的时间中点补偿网络延迟
            rtt = (t1 - t0).total_seconds()
            local_mid = t0 + timedelta(seconds=rtt / 2)
            offset = (server_time - local_mid).total_seconds()
            offsets.append(offset)
            logger.info(f"  第{i+1}次采样: 偏差={offset:+.3f}s, RTT={rtt*1000:.0f}ms")
        except Exception as e:
            logger.warning(f"  第{i+1}次采样失败: {e}")

    if not offsets:
        logger.error("无法同步服务器时间，将使用本地时间")
        return 0.0

    avg_offset = sum(offsets) / len(offsets)
    logger.info(f"时间同步完成，平均偏差: {avg_offset:+.3f}s（正值=服务器快）")
    return avg_offset


def now_server(offset: float) -> datetime:
    """返回当前时刻的服务器时间"""
    return datetime.now(timezone.utc) + timedelta(seconds=offset)


def server_str(offset: float) -> str:
    """返回服务器时间的字符串表示"""
    return now_server(offset).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_target_time(time_str: str) -> tuple:
    """解析目标时间字符串 'HH:MM:SS' 为 (hour, minute, second)"""
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        h, m = parts
        s = "00"
    elif len(parts) == 3:
        h, m, s = parts
    else:
        logger.error(f"时间格式错误: {time_str}，应为 HH:MM:SS 或 HH:MM")
        sys.exit(1)
    return int(h), int(m), int(s)


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
class TimedRestock:
    """定点补货抢购"""

    def __init__(self, config: dict, target_time: tuple, advance: int, dry_run: bool = False):
        self.config = config
        self.target_h, self.target_m, self.target_s = target_time
        self.advance_seconds = advance  # 提前多少秒开始预热
        self.dry_run = dry_run
        self.user_data_dir = Path(config["browser"].get("user_data_dir", "./browser_data")).resolve()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.selectors = config.get("selectors", {})
        self.time_offset = 0.0
        self.purchase_attempted = False

    # ---- 浏览器 ----
    def launch_browser(self) -> tuple:
        """
        启动浏览器。
        - 优先使用系统 Chrome（指纹更正常）
        - 关键阶段 slow_mo=0，不做人为延迟
        - 随机视口尺寸，不固定 1366x768
        """
        channel, channel_desc = get_browser_channel()
        if channel:
            logger.info(f"使用系统浏览器: {channel_desc}（指纹更接近正常用户）")
        else:
            logger.warning("未检测到系统 Chrome/Edge，使用 Playwright 自带 Chromium")
            logger.warning("自带 Chromium 缺少插件/证书/历史，检测风险较高，建议安装 Chrome")

        p = sync_playwright().start()
        viewport = get_common_viewport()
        logger.info(f"视口: {viewport['width']}x{viewport['height']}")

        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": False,
            "slow_mo": 0,  # 关键时刻零延迟
            "viewport": viewport,
            "locale": "zh-CN",
            "args": get_browser_args(),
        }
        if channel:
            launch_kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        context.add_init_script(PAGE_INIT_SCRIPT)

        self._playwright = p
        self._context = context
        return context

    def create_page(self, context: BrowserContext) -> Page:
        return context.new_page()

    # ---- 登录 ----
    def ensure_logged_in(self, page: Page) -> bool:
        """确保已登录"""
        page.goto("https://www.jd.com/", wait_until="domcontentloaded")
        time.sleep(1)

        try:
            logged_in_el = page.locator('.nickname, .user-name, .J_user, .user_pro a').first
            if logged_in_el.count() > 0:
                text = logged_in_el.inner_text(timeout=3000)
                if text and "登录" not in text:
                    logger.info(f"已登录用户: {text.strip()}")
                    return True
        except Exception:
            pass

        login_link = page.locator('text=你好，请登录').first
        if login_link.count() == 0:
            login_link = page.locator('.link-login').first

        if login_link.count() > 0:
            logger.info("=" * 50)
            logger.info("请手动扫码登录（90秒超时）...")
            logger.info("=" * 50)
            login_link.click()
            try:
                page.wait_for_url("**/www.jd.com/**", timeout=90_000)
                page.wait_for_selector('.nickname, .user-name, .J_user', timeout=30_000)
                logger.info("登录成功")
                return True
            except PlaywrightTimeout:
                logger.error("登录超时")
                return False

        return True

    # ---- 预热阶段 ----
    def pre_warm(self, page: Page, product: dict):
        """
        预热：在目标时间前完成所有可以提前的操作。
        使用自然浏览路径（首页→商品页），而不是直接跳到商品页。
        只在预热阶段「表演」——真正抢购时不拖慢速度。
        """
        name = product["name"]
        url = product["url"]
        size = product.get("size", "")
        color = product.get("color", "")
        quantity = product.get("quantity", 1)

        logger.info(f"[{name}] === 预热阶段 ===")

        # 1. 自然浏览路径：首页 → 浏览 → 商品页（带 referrer）
        #    直接打开商品页 + 立刻下单是明显的 bot 特征
        logger.info(f"[{name}] 模拟自然浏览路径...")
        warm_browsing_session(page, url)

        # 2. 在商品页正常浏览
        logger.info(f"[{name}] 浏览商品详情...")
        human_scroll(page, target_y=random.randint(300, 500))
        human_delay(0.5, 1.5)
        human_scroll(page, target_y=random.randint(600, 900))
        human_delay(0.5, 1.0)

        # 3. 预选 SKU（颜色/尺码）
        if color:
            self._try_select_sku(page, product, color)
        if size:
            self._try_select_sku(page, product, size)

        human_delay(0.5, 1.0)

        # 4. 预填数量
        if quantity > 1:
            try:
                qty_input = page.locator("#buy-num, .buy-num input, .quantity input").first
                if qty_input.count() > 0:
                    qty_input.fill(str(quantity))
            except Exception:
                pass

        # 5. 检测当前按钮状态，决定抢购策略
        page_status = self._detect_page_status(page, product)
        logger.info(f"[{name}] 当前页面状态: {page_status}")

        self.page_status = page_status
        self.pre_warmed = True

    def _try_select_sku(self, page: Page, product: dict, value: str):
        """尝试预选一个 SKU 值"""
        name = product["name"]
        selectors = [
            f"[data-value='{value}']",
            f"a[title='{value}']",
            f"li[title='{value}']",
            f"a:has-text('{value}')",
            f"li:has-text('{value}')",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    cls = el.get_attribute("class") or ""
                    if "selected" not in cls.lower() and "cur" not in cls.lower():
                        el.click()
                        time.sleep(0.3)
                    return
            except Exception:
                continue

    def _detect_page_status(self, page: Page, product: dict) -> str:
        """
        检测页面当前状态，决定抢购策略。
        可能的状态：
          - 'countdown'    : 有倒计时（秒杀/预售场景），等计时归零按钮自动变化
          - 'out_of_stock' : 无货，需要刷新等待按钮变化
          - 'in_stock'     : 当前就有货可以直接买
          - 'unknown'      : 无法判断
        """
        body = page.locator("body").inner_text()

        # 倒计时场景
        counter_indicators = ["即将开始", "距离开始", "倒计时", "countdown"]
        if any(kw in body for kw in counter_indicators):
            logger.info("  检测到倒计时/即将开始的标记，使用「等待按钮变化」策略")
            return "countdown"

        # 无货场景
        oos_indicators = ["到货通知", "暂时无货", "无货", "已售罄"]
        if any(kw in body for kw in oos_indicators):
            logger.info("  检测到无货标记，使用「刷新页面」策略")
            return "out_of_stock"

        # 有货
        buy_indicators = ["加入购物车", "立即抢购", "立即购买", "立即预定", "马上抢"]
        if any(kw in body for kw in buy_indicators):
            logger.info("  检测到购买按钮，当前有货")
            return "in_stock"

        return "unknown"

    # ---- 等待目标时间 ----
    def wait_until_fire_time(self) -> bool:
        """
        等待直到目标时间前 0.3 秒。
        期间每秒打印剩余时间。如果用户提前退出则返回 False。
        """
        logger.info("=" * 50)
        logger.info(f"目标时间: {self.target_h:02d}:{self.target_m:02d}:{self.target_s:02d}")
        logger.info(f"当前服务器时间: {server_str(self.time_offset)}")
        logger.info("等待中，可按 Ctrl+C 取消...")
        logger.info("=" * 50)

        while True:
            now = now_server(self.time_offset)
            target = now.replace(
                hour=self.target_h,
                minute=self.target_m,
                second=self.target_s,
                microsecond=0,
            )

            remaining = (target - now).total_seconds()

            if remaining <= -5:
                # 已经超过目标时间 5 秒，太晚了
                logger.warning("目标时间已过 5 秒以上，可能已错过")
                return True  # 仍然尝试

            if remaining <= 0.3:
                # 距离目标时间不到 0.3 秒，立即行动
                logger.info(f"倒计时: {remaining:.1f}s → 准备执行！")
                return True

            # 打印倒计时
            if remaining > 10:
                if int(remaining) % 10 == 0:
                    logger.info(f"距离目标还有 {int(remaining)} 秒...")
            elif remaining > 3:
                logger.info(f"距离目标还有 {remaining:.1f} 秒...")
            else:
                logger.info(f"!!! {remaining:.2f}s !!!")

            time.sleep(0.1)  # 最后阶段密集检查

        return True

    # ---- 执行抢购 ----
    def execute_buy(self, page: Page, product: dict):
        """在目标时间点执行抢购操作"""
        name = product["name"]

        logger.info(f"[{name}] === 执行抢购 ===")
        logger.info(f"服务器时间: {server_str(self.time_offset)}")
        t_start = time.perf_counter()

        if self.page_status == "countdown":
            self._buy_countdown_strategy(page, product)
        else:
            self._buy_refresh_strategy(page, product)

        elapsed = time.perf_counter() - t_start
        logger.info(f"[{name}] 抢购动作耗时: {elapsed:.2f}s")
        self.purchase_attempted = True

    def _buy_countdown_strategy(self, page: Page, product: dict):
        """
        倒计时策略：页面有倒计时器，按钮会自动从「即将开始」变为「立即抢购」。
        我们只需等待按钮出现然后立即点击。
        """
        name = product["name"]
        logger.info(f"[{name}] 策略：等待按钮自动变为可购买状态")

        # 这些是京东常见的抢购按钮选择器
        buy_buttons = [
            "#btn-reservation",      # 立即预定
            ".btn-reservation",
            "#btn-buy",              # 立即购买
            "#InitCartUrl",          # 加入购物车
            "#btn-addcart",
            ".btn-addtocart",
            "a:has-text('立即抢购')",
            "a:has-text('马上抢')",
            "button:has-text('立即抢购')",
            "button:has-text('马上抢')",
            "a:has-text('立即购买')",
            "a:has-text('加入购物车')",
        ]

        clicked = False
        deadline = time.time() + 10  # 最多等 10 秒

        while time.time() < deadline:
            for sel in buy_buttons:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        cls = (btn.get_attribute("class") or "").lower()
                        text = (btn.inner_text(timeout=500) or "").strip()

                        # 确认按钮可用（不是灰色禁用态）
                        disabled = "disable" in cls or "disabled" in cls
                        is_buy = any(kw in text for kw in [
                            "立即抢购", "马上抢", "立即购买", "加入购物车", "立即预定"
                        ])

                        if is_buy and not disabled:
                            logger.info(f"[{name}] 检测到可点击按钮: '{text}' (选择器: {sel})")
                            btn.click()
                            clicked = True
                            break
                except Exception:
                    continue

            if clicked:
                break
            time.sleep(0.05)  # 50ms 轮询

        if not clicked:
            logger.warning(f"[{name}] 10秒内未检测到可购买按钮，尝试刷新页面...")
            self._buy_refresh_strategy(page, product)
            return

        self._after_buy_click(page, product)

    def _buy_refresh_strategy(self, page: Page, product: dict):
        """
        刷新策略：当前显示无货，需要在目标时间点刷新页面，
        然后抢在第一时间点击出现的购买按钮。
        """
        name = product["name"]
        logger.info(f"[{name}] 策略：刷新页面后抢按钮")

        # 快速刷新（只等初始 HTML，不等完整渲染）
        page.reload(wait_until="commit")
        logger.info(f"[{name}] 页面已刷新，立即搜索购买按钮...")

        # 轮询检测按钮（50ms 间隔，最快速度）
        buy_buttons = [
            "#InitCartUrl",
            ".btn-addtocart",
            "#btn-addcart",
            "#btn-buy",
            "a:has-text('加入购物车')",
            "a:has-text('立即抢购')",
            "a:has-text('马上抢')",
            "button:has-text('加入购物车')",
        ]

        clicked = False
        deadline = time.time() + 8

        while time.time() < deadline:
            for sel in buy_buttons:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        cls = (btn.get_attribute("class") or "").lower()
                        text = (btn.inner_text(timeout=200) or "").strip()

                        disabled = "disable" in cls or "disabled" in cls
                        is_buy = any(kw in (text + cls) for kw in [
                            "加入购物车", "立即抢购", "马上抢", "立即购买", "addtocart"
                        ])

                        if is_buy and not disabled:
                            logger.info(f"[{name}] 检测到按钮: '{text}'")
                            btn.click()
                            clicked = True
                            break
                except Exception:
                    continue

            if clicked:
                break
            time.sleep(0.05)

        if not clicked:
            logger.error(f"[{name}] 刷新后仍未能找到购买按钮（可能已秒光）")

        self._after_buy_click(page, product)

    def _after_buy_click(self, page: Page, product: dict):
        """点击购买按钮后的后续操作"""
        name = product["name"]

        if self.dry_run:
            logger.info(f"[{name}] [DRY-RUN] 模拟到此结束，不执行后续操作")
            return

        # 等待页面响应
        time.sleep(0.8)

        # 尝试直接进入结算（某些按钮点击后会直接跳转）
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeout:
            pass

        current_url = page.url
        logger.info(f"[{name}] 当前 URL: {current_url}")

        # 判断当前在哪个页面
        if "cart.jd.com" in current_url:
            # 在购物车页面，需要点结算
            self._checkout_from_cart(page, product)
        elif any(kw in current_url for kw in ["order.jd.com", "trade.jd.com", "marathon.jd.com"]):
            # 已经在订单确认页
            logger.info(f"[{name}] 已进入订单确认页面")
            self._try_submit_order(page)
        elif "success" in current_url.lower() or "added" in current_url.lower():
            # 加购成功弹窗
            logger.info(f"[{name}] 加购成功，前往购物车结算")
            try:
                go_cart = page.locator("text=去购物车结算, text=去结算").first
                if go_cart.count() > 0:
                    go_cart.click()
                    time.sleep(1.5)
                    self._checkout_from_cart(page, product)
            except Exception:
                page.goto("https://cart.jd.com/cart_index/")
                time.sleep(1)
                self._checkout_from_cart(page, product)
        else:
            # 不确定状态，尝试导航到购物车
            logger.info(f"[{name}] 状态不明确，尝试前往购物车...")
            page.goto("https://cart.jd.com/cart_index/", wait_until="domcontentloaded")
            time.sleep(1)
            self._checkout_from_cart(page, product)

    def _checkout_from_cart(self, page: Page, product: dict):
        """在购物车页面勾选商品并结算"""
        name = product["name"]
        logger.info(f"[{name}] 购物车结算...")

        # 勾选商品
        checkbox_selectors = [
            ".item-checkbox input",
            "input[name='selectItem']",
            ".cart-checkbox",
        ]
        for sel in checkbox_selectors:
            try:
                cbs = page.locator(sel)
                for i in range(cbs.count()):
                    cb = cbs.nth(i)
                    if not cb.is_checked():
                        cb.check()
                        time.sleep(0.1)
                if cbs.count() > 0:
                    logger.info(f"[{name}] 已勾选 {cbs.count()} 件")
                    break
            except Exception:
                continue

        time.sleep(0.3)

        # 点击结算
        checkout_selectors = [
            ".common-submit-btn",
            "#J_Go",
            "#settleup",
            "a:has-text('去结算')",
            "button:has-text('去结算')",
        ]
        for sel in checkout_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    time.sleep(2)
                    page.wait_for_load_state("domcontentloaded")
                    logger.info(f"[{name}] 已进入结算页面")
                    break
            except Exception:
                continue

        self._try_submit_order(page)

    def _try_submit_order(self, page: Page):
        """在订单确认页尝试提交订单"""
        # 模拟真人的确认行为：看看地址、翻翻价格、停顿一下
        # 直接秒点提交是不自然的
        order_page_ritual(page)

        submit_selectors = [
            "#submitOrder",
            "#submit",
            ".checkout-submit button",
            "button:has-text('提交订单')",
            "a:has-text('提交订单')",
        ]

        auto_submit = self.config.get("checkout", {}).get("auto_submit_order", False)
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    if auto_submit:
                        btn.click()
                        logger.info("已点击「提交订单」")
                        logger.info("=" * 50)
                        logger.info("请手动完成支付（密码/指纹/人脸）")
                        logger.info("=" * 50)
                    else:
                        logger.info("=" * 50)
                        logger.info("已到达订单确认页，请在浏览器中点击「提交订单」并完成支付")
                        logger.info("脚本会保持运行，完成后手动关闭或 Ctrl+C 退出")
                        logger.info("=" * 50)
                    return
            except Exception:
                continue

        logger.info("已到达结算页面，请手动完成后续操作")

    # ---- 主流程 ----
    def run(self):
        """主流程"""
        # 1. 时间同步
        self.time_offset = sync_server_time()
        logger.info(f"时间偏差已记录: {self.time_offset:+.3f}s")

        # 2. 启动浏览器
        context = self.launch_browser()
        login_page = self.create_page(context)

        # 3. 登录
        if not self.ensure_logged_in(login_page):
            logger.error("登录失败")
            return
        login_page.close()

        # 4. 选择要购买的商品
        products = [p for p in self.config.get("products", []) if p.get("enabled", True)]
        if not products:
            logger.error("没有启用的商品")
            return

        # 对定时抢购场景，通常只抢一个商品，这里取第一个
        product = products[0]
        logger.info(f"目标商品: {product['name']}")

        # 5. 预热
        buy_page = self.create_page(context)
        self.pre_warm(buy_page, product)

        # 6. 等待目标时间
        logger.info(f"预热完成，等待目标时间 {self.target_h:02d}:{self.target_m:02d}:{self.target_s:02d}...")
        logger.info(f"当前服务器时间: {server_str(self.time_offset)}")

        if not self.wait_until_fire_time():
            logger.info("用户取消")
            return

        # 7. 执行抢购
        self.execute_buy(buy_page, product)

        # 8. 保持浏览器打开，让用户完成支付
        logger.info("脚本保持运行，浏览器不会关闭。")
        logger.info("完成操作后按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            context.close()
            logger.info("已退出")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="京东定时补货抢购脚本")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件路径")
    parser.add_argument(
        "--time", "-t",
        required=True,
        help="目标补货时间，格式 HH:MM 或 HH:MM:SS（如 08:00 或 08:00:00）",
    )
    parser.add_argument(
        "--advance", "-a",
        type=int,
        default=30,
        help="提前多少秒开始预热（默认30秒）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只模拟，不实际下单")
    args = parser.parse_args()

    # 切换到脚本目录
    os.chdir(Path(__file__).parent.resolve())

    config = load_config(args.config)
    target = parse_target_time(args.time)
    advance = max(args.advance, 15)  # 最少提前15秒

    logger.info("=" * 50)
    logger.info("京东定时补货抢购脚本")
    logger.info(f"目标时间: {args.time}")
    logger.info(f"预热提前: {advance} 秒")
    logger.info(f"Dry-run: {args.dry_run}")
    logger.info(f"日志文件: {log_file}")
    logger.info("=" * 50)

    # 检查目标时间是否在未来
    now_local = datetime.now()
    target_dt = now_local.replace(
        hour=target[0], minute=target[1], second=target[2], microsecond=0
    )
    if target_dt < now_local:
        logger.warning(f"目标时间 {args.time} 已过，脚本仍会运行但可能立即尝试下单")
        logger.warning("如果这不是你想要的，请 Ctrl+C 退出并指定未来的时间")
        time.sleep(3)

    bot = TimedRestock(config, target, advance, dry_run=args.dry_run)
    bot.run()


if __name__ == "__main__":
    main()
