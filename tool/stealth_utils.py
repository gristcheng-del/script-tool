"""
京东自动化 — 反检测工具集
==========================
目标：让脚本行为更接近真人，降低被风控标记的概率。

设计原则：
  - 只做行为模拟，不做指纹伪造（前者是「像人」，后者是「伪装」）
  - 抢占关键时刻不拖慢速度，只在预热/闲时使用
  - 每次运行的参数有差异，不形成固定模式

注意：这些是通用的基础手段，不能保证 100% 不触发检测。
      如果京东升级了检测策略，可能需要调整。
"""

import random
import time
import math
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 系统 Chrome 检测
# ---------------------------------------------------------------------------
def get_browser_channel() -> Tuple[str, Optional[str]]:
    """
    检测系统中可用的浏览器，优先使用系统安装的 Chrome/Edge。
    系统浏览器的指纹比 Playwright 自带的 Chromium 正常得多——
    有你的插件、证书、字体、浏览历史。

    返回: (channel_name, fallback_description)
    """
    if sys.platform != "win32":
        return "chrome", None

    try:
        import winreg

        # 优先 Chrome
        for name, reg_path in [
            ("chrome", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            ("msedge", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ]:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                path = winreg.QueryValue(key, None)
                winreg.CloseKey(key)
                if path and Path(path).exists():
                    return name, Path(path).name
            except Exception:
                continue
    except Exception:
        pass

    # 直接检查常见路径
    chrome_paths = [
        Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    for p in chrome_paths:
        if p.exists():
            return "chrome", "Chrome (by path)"

    return None, "bundled Chromium"


# ---------------------------------------------------------------------------
# 浏览器上下文配置
# ---------------------------------------------------------------------------
def get_browser_args() -> list:
    """
    返回启动参数。关键点：
      - 不传 --disable-blink-features=AutomationControlled（这个参数本身就是一个信号）
      - 不传 --disable-gpu 等明显是无头模式的参数
      - 传正常的语言、时区、屏幕信息
    """
    return [
        "--lang=zh-CN",
        "--no-sandbox",  # 某些 Windows 环境需要
    ]


def get_common_viewport() -> dict:
    """返回一个随机的常见屏幕尺寸"""
    # 故意不用 1366x768——这个分辨率在 bot 中太常见
    choices = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1536, "height": 864},
        {"width": 1680, "height": 1050},
        {"width": 1600, "height": 900},
    ]
    return random.choice(choices)


PAGE_INIT_SCRIPT = """
// 只处理最基础的 webdriver 标记。
// 更深的指纹（如 CDP runtime、chrome 对象差异）无法通过 JS 修改。
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 伪造 plugins 数组长度（Playwright 默认可能为空）
if (!navigator.plugins || navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            plugins.item = (i) => plugins[i] || null;
            plugins.namedItem = (name) => plugins.find(p => p.name === name) || null;
            plugins.refresh = () => {};
            Object.setPrototypeOf(plugins, PluginArray.prototype);
            return plugins;
        },
        enumerable: true,
        configurable: true,
    });
}

// 伪造 languages（空数组是明显特征）
if (!navigator.languages || navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en-US', 'en'],
    });
}
"""


# ---------------------------------------------------------------------------
# 人类化延迟
# ---------------------------------------------------------------------------
def human_delay(min_s: float = 0.3, max_s: float = 2.0):
    """
    使用 Gamma 分布而不是均匀分布。
    真人的操作间隔不是均匀的——大部分间隔较短，偶尔会停顿较久（看手机、喝水等）。

    Gamma 分布 shape=2 产生的效果：
      - 大部分延迟集中在均值附近
      - 有自然的右尾（偶尔的长延迟）
      - 不会有机械的固定间隔感
    """
    mean = (min_s + max_s) / 2
    # Gamma(shape=2, scale=mean/2) 产生集中在 mean 附近的分布
    delay = random.gammavariate(alpha=2.0, beta=mean / 2)
    delay = max(min_s, min(delay, max_s * 1.5))
    time.sleep(delay)


def jitter(base_s: float, pct: float = 0.3) -> float:
    """给一个基准时间加上随机抖动"""
    return base_s * (1 + random.uniform(-pct, pct))


# ---------------------------------------------------------------------------
# 人类化滚动
# ---------------------------------------------------------------------------
def human_scroll(page, target_y: int = None, viewport_height: int = 768):
    """
    模拟人类滚动行为：
      - 不是一次性滚动到位，而是分 3-8 步
      - 每步之间有微小间隔
      - 偶尔往回滚一点（像在回看内容）
    """
    current_y = page.evaluate("window.scrollY")
    if target_y is None:
        target_y = current_y + random.randint(200, 600)

    steps = random.randint(4, 10)
    delta = target_y - current_y

    for i in range(steps):
        progress = (i + 1) / steps
        # ease-in-out：开始慢，中间快，结束慢
        eased = progress * progress * (3 - 2 * progress)
        next_y = current_y + int(delta * eased)
        page.evaluate(f"window.scrollTo({{top: {next_y}, behavior: 'auto'}})")
        time.sleep(random.uniform(0.02, 0.08))

    # ~15% 概率轻微回滚（真人偶尔回看）
    if random.random() < 0.15:
        time.sleep(random.uniform(0.4, 1.0))
        rollback = random.randint(30, 100)
        page.evaluate(f"window.scrollBy({{top: -{rollback}, behavior: 'auto'}})")


# ---------------------------------------------------------------------------
# 自然浏览路径（关键！）
# ---------------------------------------------------------------------------
def warm_browsing_session(page, target_product_url: str):
    """
    模拟自然浏览路径：首页 → 停留浏览 → 商品页。

    直接打开商品页 + 立刻下单是最明显的 bot 特征。
    自然的做法是先逛逛首页，再进入商品页，referrer 链完整。

    这个函数在预热阶段调用，不赶时间，可以充分「表演」。
    """
    # Step 1: 访问 JD 首页
    page.goto("https://www.jd.com/", wait_until="domcontentloaded")
    human_delay(2.0, 4.0)  # 首页加载后「浏览」一会

    # Step 2: 在首页滚动浏览
    human_scroll(page, target_y=random.randint(300, 600))
    human_delay(1.0, 3.0)

    # Step 3: 可能再往下翻翻
    if random.random() < 0.6:
        human_scroll(page, target_y=random.randint(700, 1200))
        human_delay(0.8, 2.5)

    # 偶尔在首页停留更久（看看推荐之类的）
    if random.random() < 0.3:
        human_delay(2.0, 5.0)

    # Step 4: 导航到目标商品页（带 referrer）
    page.goto(target_product_url, wait_until="domcontentloaded",
              referer="https://www.jd.com/")
    human_delay(1.5, 3.0)


# ---------------------------------------------------------------------------
# 操作间隔随机化
# ---------------------------------------------------------------------------
class IntervalRandomizer:
    """
    轮询间隔随机化。
    用于 main.py 的周期性检查——不要正好每 10 分钟检查一次，
    而是 8~12 分钟之间随机。
    """

    def __init__(self, base_minutes: int):
        self.base = base_minutes

    def next_sleep_seconds(self) -> int:
        """返回下次等待的秒数，在基准值 ±30% 范围内"""
        variation = int(self.base * 60 * random.uniform(-0.3, 0.3))
        return self.base * 60 + variation


# ---------------------------------------------------------------------------
# 订单提交前的人类化确认
# ---------------------------------------------------------------------------
def order_page_ritual(page):
    """
    在订单确认页做「正常人的确认动作」：
      - 稍微滚动看看总价
      - 停顿一下（假装在读地址）
      - 再滚动到底部点提交

    直接打开订单页就秒点提交，是不太自然的行为。
    """
    # 先看看页面顶部（地址确认）
    human_delay(0.5, 1.5)
    human_scroll(page, target_y=random.randint(200, 400))
    human_delay(0.8, 2.0)

    # 滚动去看看总价
    human_scroll(page, target_y=random.randint(500, 800))
    human_delay(0.5, 1.5)

    # 再滚到提交按钮
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    human_delay(0.3, 0.8)


# ---------------------------------------------------------------------------
# 工具箱
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    channel, desc = get_browser_channel()
    print(f"检测到浏览器: {channel or 'None'} ({desc or 'fallback to bundled Chromium'})")
    print(f"视口: {get_common_viewport()}")

    r = IntervalRandomizer(10)
    print("间隔随机化示例（基准 10 分钟）:", end=" ")
    for _ in range(5):
        print(f"{r.next_sleep_seconds() / 60:.1f}min", end=" ")
    print()
