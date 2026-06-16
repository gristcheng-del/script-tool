"""
京东自动补货下单脚本
====================
功能：定时监控指定商品库存，有货时自动加购物车并进入结算流程。
支付环节需要手动完成（密码/指纹/人脸验证无法自动化）。
首次使用时需要手动扫码登录，登录状态会保存到本地。

用法：
    python main.py              # 使用默认 config.json
    python main.py --once       # 只检查一轮（不循环）
    python main.py --dry-run    # 只检查库存，不加购不下单
    python main.py --config my_config.json

依赖安装：
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, time as dt_time
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
    IntervalRandomizer,
    order_page_ritual,
)

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"jd_auto_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("jd_auto")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """加载 JSON 配置文件"""
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    logger.info(f"已加载配置: {config_path}，共 {len(config.get('products', []))} 个商品")
    return config


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
class JDAutoBuy:
    """京东自动补货下单"""

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.user_data_dir = Path(config["browser"].get("user_data_dir", "./browser_data")).resolve()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.selectors = config.get("selectors", {})
        self.running = True

        # 注册信号处理，优雅退出
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info("收到退出信号，正在停止...")
        self.running = False

    # ---- 浏览器管理 ----
    def _human_delay(self, min_sec: float = 0.3, max_sec: float = 1.5):
        """
        模拟人类操作的随机延迟。
        使用 Gamma 分布替代均匀分布——更接近真人的停顿模式。
        """
        human_delay(min_sec, max_sec)

    def launch_browser(self) -> BrowserContext:
        """
        启动浏览器，使用持久化上下文以保留登录状态。
        优先使用系统 Chrome（指纹更接近正常用户）。
        """
        channel, channel_desc = get_browser_channel()
        if channel:
            logger.info(f"使用系统浏览器: {channel_desc}")
        else:
            logger.warning("未检测到系统 Chrome/Edge，指纹检测风险较高")

        p = sync_playwright().start()
        viewport = get_common_viewport()
        logger.info(f"启动浏览器，用户数据: {self.user_data_dir}，视口: {viewport['width']}x{viewport['height']}")

        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.config["browser"].get("headless", False),
            "slow_mo": self.config["browser"].get("slow_mo", 300),
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

    # ---- 登录检测 ----
    def ensure_logged_in(self, page: Page) -> bool:
        """确保已登录京东。未登录则等待用户手动扫码登录。"""
        page.goto("https://www.jd.com/", wait_until="domcontentloaded")
        self._human_delay(2, 3)

        # 检查是否已登录：查找页面上的登录状态元素
        # 已登录时页面右上角会显示用户名，未登录显示"你好，请登录"
        try:
            # 尝试多种方式检测登录状态
            logged_in = page.locator('.nickname, .user-name, .J_user, .user_pro a').first
            if logged_in.count() > 0:
                text = logged_in.inner_text(timeout=3000)
                if text and "登录" not in text:
                    logger.info(f"检测到已登录用户: {text.strip()}")
                    return True
        except Exception:
            pass

        # 检查是否需要登录
        login_link = page.locator('text=你好，请登录').first
        if login_link.count() == 0:
            login_link = page.locator('.link-login').first

        if login_link.count() > 0:
            logger.info("=" * 50)
            logger.info("检测到未登录，将在浏览器中打开登录页面")
            logger.info("请手动扫码登录（给你 120 秒时间）...")
            logger.info("=" * 50)

            login_link.click()
            self._human_delay(1, 2)

            # 等待用户完成登录，最多等 120 秒
            try:
                page.wait_for_url("**/www.jd.com/**", timeout=120_000)
                # 等待登录状态出现
                page.wait_for_selector(
                    '.nickname, .user-name, .J_user',
                    timeout=30_000
                )
                logger.info("登录成功！登录状态已保存到本地，下次无需重复登录。")
                return True
            except PlaywrightTimeout:
                logger.error("登录超时，请重试。")
                return False

        logger.info("登录状态检查通过。")
        return True

    # ---- 商品监控与下单 ----
    def check_and_buy(self, context: BrowserContext):
        """遍历所有启用的商品，检查库存并尝试下单"""
        products = [p for p in self.config.get("products", []) if p.get("enabled", True)]
        if not products:
            logger.warning("没有启用的商品，请在 config.json 中配置商品并设置 enabled: true")
            return

        page = context.new_page()

        for product in products:
            if not self.running:
                break

            name = product.get("name", "未知商品")
            url = product.get("url", "")
            if not url:
                logger.warning(f"[{name}] 未配置商品 URL，跳过")
                continue

            logger.info(f"[{name}] 开始检查库存...")
            try:
                self._process_product(page, product)
            except Exception as e:
                logger.error(f"[{name}] 处理出错: {e}", exc_info=True)

            # 商品间随机间隔
            self._human_delay(2, 5)

        page.close()

    def _process_product(self, page: Page, product: dict):
        """处理单个商品：检查库存 -> 选规格 -> 加购 -> 结算"""
        name = product["name"]
        url = product["url"]
        max_price = product.get("max_price", 0)
        size = product.get("size", "")
        color = product.get("color", "")
        quantity = product.get("quantity", 1)

        # 1. 访问商品页
        logger.info(f"[{name}] 正在访问商品页面: {url}")
        page.goto(url, wait_until="domcontentloaded")
        self._human_delay(2, 4)

        # 模拟人类浏览行为：分步滚动，偶尔回滚
        human_scroll(page, target_y=random.randint(300, 600))
        self._human_delay(0.5, 1.5)

        # 2. 检测库存状态
        in_stock = self._check_stock(page, product)
        if not in_stock:
            logger.info(f"[{name}] 当前无货，跳过")
            return

        logger.info(f"[{name}] 检测到有货！")

        # 3. 读取当前价格
        current_price = self._get_price(page, product)
        if current_price is not None:
            logger.info(f"[{name}] 当前价格: ¥{current_price}")
            if max_price > 0 and current_price > max_price:
                logger.info(f"[{name}] 价格 ¥{current_price} 超过上限 ¥{max_price}，跳过")
                return

        # 4. 选择规格（颜色/尺码）
        if color:
            self._select_sku(page, product, "color", color)
        if size:
            self._select_sku(page, product, "size", size)

        self._human_delay(1, 2)

        # 5. 设置数量
        if quantity > 1:
            self._set_quantity(page, quantity)

        # Dry-run 模式：只检查，不下单
        if self.dry_run:
            logger.info(f"[{name}] [DRY-RUN] 有货且有购买条件，但不执行下单操作")
            return

        # 6. 加入购物车
        if not self._add_to_cart(page, product):
            logger.error(f"[{name}] 加入购物车失败")
            return

        logger.info(f"[{name}] 已加入购物车")
        self._human_delay(1, 3)

        # 7. 进入购物车并结算
        if not self._go_to_checkout(page, product):
            logger.error(f"[{name}] 进入结算页失败")
            return

        logger.info(f"[{name}] 已进入订单结算页面，等待用户完成支付...")

        # 8. 尝试自动提交订单（如果配置允许）
        auto_submit = self.config.get("checkout", {}).get("auto_submit_order", False)
        if auto_submit:
            self._submit_order(page)
        else:
            logger.info(f"[{name}] auto_submit_order 为 false，请在浏览器中手动完成后续操作")
            logger.info(f"[{name}] 脚本将在你手动操作期间保持运行，完成后按 Ctrl+C 退出或等待下一轮检查")

        # 下单后等待一段时间，避免短时间内重复下单
        time.sleep(30)

    # ---- 库存检测 ----
    def _check_stock(self, page: Page, product: dict) -> bool:
        """检测商品是否有货"""
        selectors = self.selectors
        name = product["name"]

        # 方法1：检查"加入购物车"按钮是否可用
        add_cart_selectors = [
            selectors.get("add_to_cart_btn", "#InitCartUrl"),
            "#InitCartUrl",
            ".btn-addtocart",
            "#btn-addcart",
            "a:has-text('加入购物车')",
        ]
        for sel in add_cart_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    text = btn.inner_text(timeout=2000).strip()
                    classes = btn.get_attribute("class") or ""
                    # 如果按钮存在且不是灰色/禁用状态，认为有货
                    if "disable" not in classes.lower() and "无货" not in text:
                        logger.debug(f"[{name}] 通过选择器 {sel} 判定有货")
                        return True
            except Exception:
                continue

        # 方法2：检查无货标记
        out_of_stock_indicators = [
            selectors.get("stock_unavailable", ""),
            ".btn-notify",
            "text=到货通知",
            "text=无货",
            "text=暂时无货",
            ".itemover-tips",
        ]
        for indicator in out_of_stock_indicators:
            if not indicator:
                continue
            try:
                el = page.locator(indicator).first
                if el.count() > 0 and el.is_visible():
                    logger.debug(f"[{name}] 检测到无货标记: {indicator}")
                    return False
            except Exception:
                continue

        # 方法3：检查库存相关文本
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
            if any(kw in body_text for kw in ["到货通知", "暂时无货", "此商品暂时无货"]):
                return False
            if "加入购物车" in body_text:
                return True
        except Exception:
            pass

        # 无法确定，保守返回 False
        logger.warning(f"[{name}] 无法确定库存状态，假定无货（避免无效操作）")
        return False

    # ---- 价格读取 ----
    def _get_price(self, page: Page, product: dict) -> Optional[float]:
        """读取商品当前价格"""
        price_selectors = [
            self.selectors.get("price_current", ""),
            ".p-price .price",
            ".summary-price .price",
            "#jd-price",
            ".J-p-",
            "span.price",
        ]
        for sel in price_selectors:
            if not sel:
                continue
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    text = el.inner_text(timeout=2000).strip()
                    nums = re.findall(r"[\d.]+", text)
                    if nums:
                        return float(nums[0])
            except Exception:
                continue
        return None

    # ---- SKU 选择 ----
    def _select_sku(self, page: Page, product: dict, sku_type: str, value: str):
        """选择商品规格（颜色/尺码）"""
        name = product["name"]
        logger.info(f"[{name}] 选择{sku_type}: {value}")

        # 京东 SKU 通常用 <a> 或 <li> 标签，带 data-value 或 title 属性
        sku_selectors = [
            f"[data-value='{value}']",
            f"a[title='{value}']",
            f"li[title='{value}']",
            f"a:has-text('{value}')",
            f"li:has-text('{value}')",
            f"span:has-text('{value}')",
        ]

        for sel in sku_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    # 检查是否已被选中（已选中的通常有特殊 class）
                    cls = el.get_attribute("class") or ""
                    if "selected" in cls.lower() or "cur" in cls.lower():
                        logger.info(f"[{name}] {sku_type} '{value}' 已处于选中状态")
                        return

                    el.click()
                    self._human_delay(1, 2)
                    logger.info(f"[{name}] 已选择{sku_type}: {value}")
                    return
            except Exception:
                continue

        logger.warning(f"[{name}] 未能选择{sku_type} '{value}'，请手动确认")

    # ---- 数量设置 ----
    def _set_quantity(self, page: Page, quantity: int):
        """设置购买数量"""
        try:
            qty_input = page.locator("#buy-num, .buy-num input, .quantity input").first
            if qty_input.count() > 0:
                qty_input.fill(str(quantity))
                self._human_delay(0.5, 1)
        except Exception as e:
            logger.warning(f"设置数量失败: {e}")

    # ---- 加入购物车 ----
    def _add_to_cart(self, page: Page, product: dict) -> bool:
        """点击加入购物车"""
        name = product["name"]
        add_cart_selectors = [
            self.selectors.get("add_to_cart_btn", ""),
            "#InitCartUrl",
            ".btn-addtocart",
            "#btn-addcart",
            "a:has-text('加入购物车')",
            "button:has-text('加入购物车')",
        ]

        for sel in add_cart_selectors:
            if not sel:
                continue
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    self._human_delay(2, 4)

                    # 等待加购成功的反馈
                    try:
                        # 加购成功通常会弹出提示或跳转
                        page.wait_for_selector(
                            "text=已加入购物车, text=加入成功, .ui-dialog",
                            timeout=5000,
                        )
                        logger.info(f"[{name}] 加购操作已触发")
                    except PlaywrightTimeout:
                        pass
                    return True
            except Exception as e:
                logger.debug(f"[{name}] 选择器 {sel} 失败: {e}")
                continue

        return False

    # ---- 进入结算 ----
    def _go_to_checkout(self, page: Page, product: dict) -> bool:
        """从商品页进入购物车，勾选商品，点击结算"""
        name = product["name"]
        current_url = page.url

        # 方法1：加购后可能弹出的"去购物车结算"按钮
        try:
            go_cart = page.locator(
                "text=去购物车结算, text=去购物车, #GotoShoppingCart, .go-to-cart"
            ).first
            if go_cart.count() > 0 and go_cart.is_visible():
                go_cart.click()
                self._human_delay(2, 3)
        except Exception:
            pass

        # 方法2：如果没跳转，直接导航到购物车
        if "cart.jd.com" not in page.url:
            page.goto("https://cart.jd.com/cart_index/", wait_until="domcontentloaded")
            self._human_delay(2, 4)

        # 在购物车页面：勾选商品
        self._select_cart_items(page, product)

        # 点击"去结算"
        checkout_selectors = [
            self.selectors.get("cart_checkout_btn", ""),
            ".common-submit-btn",
            "#J_Go",
            "#settleup",
            "a:has-text('去结算')",
            "button:has-text('去结算')",
        ]
        for sel in checkout_selectors:
            if not sel:
                continue
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    self._human_delay(3, 5)
                    # 等待跳转到结算/订单确认页
                    page.wait_for_load_state("domcontentloaded")
                    logger.info(f"[{name}] 已跳转到结算页面")
                    return True
            except Exception as e:
                logger.debug(f"结算按钮 {sel}: {e}")
                continue

        logger.warning(f"[{name}] 未能找到结算按钮，当前 URL: {page.url}")
        return False

    def _select_cart_items(self, page: Page, product: dict):
        """在购物车页面勾选需要结算的商品"""
        name = product["name"]
        # 京东购物车中，每个商品有 checkbox
        checkbox_selectors = [
            self.selectors.get("cart_checkbox", ""),
            ".item-checkbox input",
            ".cart-checkbox",
            "input[name='selectItem']",
        ]
        for sel in checkbox_selectors:
            if not sel:
                continue
            try:
                checkboxes = page.locator(sel)
                count = checkboxes.count()
                for i in range(count):
                    cb = checkboxes.nth(i)
                    if not cb.is_checked():
                        cb.check()
                        self._human_delay(0.3, 0.8)
                if count > 0:
                    logger.info(f"[{name}] 已勾选购物车中 {count} 件商品")
                    return
            except Exception as e:
                logger.debug(f"勾选商品失败: {e}")

        # 如果找不到 checkbox，尝试"全选"
        try:
            select_all = page.locator(".select-all, #select-all, .checkall").first
            if select_all.count() > 0:
                select_all.click()
                self._human_delay(0.5, 1)
                logger.info(f"[{name}] 已全选购物车商品")
        except Exception:
            pass

    # ---- 提交订单 ----
    def _submit_order(self, page: Page):
        """在订单确认页点击提交订单"""
        try:
            # 等待订单确认页加载
            page.wait_for_load_state("domcontentloaded")

            # 模拟真人确认行为：看看地址、翻翻价格
            order_page_ritual(page)

            submit_selectors = [
                self.selectors.get("order_submit_btn", ""),
                "#submitOrder",
                "#submit",
                ".checkout-submit button",
                "button:has-text('提交订单')",
                "a:has-text('提交订单')",
            ]
            for sel in submit_selectors:
                if not sel:
                    continue
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        logger.info("已点击「提交订单」")
                        self._human_delay(3, 5)
                        logger.info("=" * 50)
                        logger.info("如果页面跳转到支付页面，请手动完成支付")
                        logger.info("（密码/指纹/人脸验证无法自动化）")
                        logger.info("=" * 50)
                        return
                except Exception as e:
                    logger.debug(f"提交按钮 {sel}: {e}")
            logger.warning("未找到提交订单按钮，请手动操作")
        except Exception as e:
            logger.error(f"提交订单出错: {e}")

    # ---- 调度运行 ----
    def run(self):
        """主运行循环"""
        logger.info("=" * 50)
        logger.info("京东自动补货脚本启动")
        logger.info(f"日志文件: {log_file}")
        logger.info(f"用户数据: {self.user_data_dir}")
        logger.info(f"Dry-run: {self.dry_run}")
        logger.info("=" * 50)

        context = self.launch_browser()
        page = context.new_page()

        try:
            # 首次登录
            if not self.ensure_logged_in(page):
                logger.error("登录失败，退出")
                return
            page.close()

            # 调度循环（使用随机化间隔，避免固定模式的定时任务被识别）
            check_interval = self.config["schedule"].get("check_interval_minutes", 10)
            active_hours = self.config["schedule"].get("active_hours", [8, 23])
            interval_rng = IntervalRandomizer(check_interval)

            logger.info(f"开始监控循环，基准间隔 {check_interval} 分钟（实际 ±30% 随机）")
            logger.info(f"活跃时段: {active_hours[0]}:00 - {active_hours[1]}:00")

            while self.running:
                now = datetime.now()
                current_hour = now.hour

                # 检查是否在活跃时段
                if active_hours[0] <= current_hour < active_hours[1]:
                    logger.info("-" * 40)
                    logger.info(f"轮次开始 - {now.strftime('%Y-%m-%d %H:%M:%S')}")
                    self.check_and_buy(context)
                else:
                    logger.debug(f"当前时间 {current_hour}:00 不在活跃时段，等待...")

                # 等待下一轮（间隔随机化，避免固定模式被识别）
                sleep_sec = interval_rng.next_sleep_seconds()
                logger.info(f"等待 {sleep_sec / 60:.1f} 分钟后进行下一轮检查...")
                for _ in range(sleep_sec):
                    if not self.running:
                        break
                    time.sleep(1)

        except KeyboardInterrupt:
            logger.info("用户中断")
        finally:
            logger.info("正在关闭浏览器...")
            try:
                context.close()
            except Exception:
                pass
            logger.info("脚本已退出")

    def run_once(self):
        """只运行一轮检查"""
        logger.info("=" * 50)
        logger.info("京东自动补货脚本 - 单次检查模式")
        logger.info(f"日志文件: {log_file}")
        logger.info(f"Dry-run: {self.dry_run}")
        logger.info("=" * 50)

        context = self.launch_browser()
        page = context.new_page()

        try:
            if not self.ensure_logged_in(page):
                logger.error("登录失败，退出")
                return
            page.close()
            self.check_and_buy(context)
        finally:
            logger.info("按 Enter 关闭浏览器...")
            input()
            context.close()
            logger.info("脚本已退出")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="京东自动补货下单脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python main.py                    # 循环监控模式
  python main.py --once             # 只检查一轮
  python main.py --dry-run          # 只检查库存，不下单
  python main.py --once --dry-run   # 单次库存检查
        """,
    )
    parser.add_argument(
        "--config", "-c",
        default="config.json",
        help="配置文件路径 (默认: config.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只运行一轮检查，不循环",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式：只检查库存，不执行加购/下单操作",
    )
    args = parser.parse_args()

    # 切换到脚本所在目录，确保相对路径正确
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)

    config = load_config(args.config)
    bot = JDAutoBuy(config, dry_run=args.dry_run)

    if args.once:
        bot.run_once()
    else:
        bot.run()


if __name__ == "__main__":
    main()
