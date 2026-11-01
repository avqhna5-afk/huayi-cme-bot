"""浏览器会话：反指纹启动、验证码登录、登录态持久化。

关键点（踩过的坑）：
1. 必须隐藏 navigator.webdriver，否则平台把观看时长判为无效。
2. 必须伪装 visibilityState=visible / hidden=false，否则标签页后台化时
   播放器不再上报进度（表现为「学了但进度不动」）。
3. 登录页密码输入是伪装输入框，要先点 #type_pwd 让真密码框显示，
   同时把值写进 #txt_user_pwd_real。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
window.chrome = window.chrome || {runtime: {}};
window.alert = function () {};
Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
"""

LAUNCH_ARGS = [
    "--disable-popup-blocking",
    "--no-first-run",
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]


class Session:
    """负责启动 Chrome、维持登录态、提供验证码识别。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.profile = Path(cfg["profile_dir"])
        self.storage = Path(cfg["state_file"]).parent / "storage_state.json"
        self._pw = None
        self.ctx: Optional[BrowserContext] = None
        self._ocr = None

    # ---------- OCR ----------
    def ocr(self):
        if self._ocr is None:
            import ddddocr  # 延迟导入，未安装时只有用到验证码才会报错

            self._ocr = ddddocr.DdddOcr(show_ad=False)
        return self._ocr

    def read_captcha(self, page: Page, img_sel: str, refresh_js: Optional[str] = None) -> Optional[str]:
        """截图识别 5 位数字验证码，失败重试 3 次。"""
        if refresh_js:
            try:
                page.evaluate(refresh_js)
            except Exception:
                pass
        time.sleep(1.2)
        for _ in range(3):
            try:
                el = page.locator(img_sel)
                el.wait_for(state="visible", timeout=6000)
                code = self.ocr().classification(el.screenshot()).strip()
                if code.isdigit() and len(code) == 5:
                    return code
            except Exception:
                pass
            time.sleep(1.2)
        return None

    # ---------- 生命周期 ----------
    def __enter__(self) -> "Session":
        self._pw = sync_playwright().start()
        self.profile.mkdir(parents=True, exist_ok=True)
        lock = self.profile / "SingletonLock"
        if lock.exists():
            try:
                lock.unlink()
            except Exception:
                pass
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile),
            channel="chrome",
            headless=bool(self.cfg.get("headless")),
            args=LAUNCH_ARGS,
            viewport=None,
        )
        self.ctx.add_init_script(STEALTH_JS)
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def save_storage(self) -> None:
        try:
            self.ctx.storage_state(path=str(self.storage))
        except Exception:
            pass

    # ---------- 登录 ----------
    def is_logged_in(self, page: Page) -> bool:
        try:
            body = page.inner_text("body")[:4000]
            if "退出登录" in body or "我的继教" in body or "我的学习记录" in body:
                return True
            return not page.evaluate("() => !!document.getElementById('txt_user_name')")
        except Exception:
            return False

    def login(self, page: Page) -> bool:
        page.goto(f"{self.base}/secure/login.aspx", wait_until="domcontentloaded", timeout=45000)
        time.sleep(2.5)
        try:  # 密码框是伪装框，先点一下才出现真实输入框
            page.evaluate("() => { const t = document.getElementById('type_pwd'); if (t) t.click(); }")
            time.sleep(0.8)
        except Exception:
            pass

        for attempt in range(10):
            try:
                page.evaluate(
                    """(d) => {
                        const setV = (el, v) => {
                            el.value = v;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        };
                        const u = document.getElementById('txt_user_name');
                        if (u) setV(u, d.u);
                        const p = document.getElementById('txt_user_pwd');
                        if (p) setV(p, d.p);
                        const pr = document.getElementById('txt_user_pwd_real');
                        if (pr) setV(pr, d.p);
                        const cb = document.querySelector('input[name=chec1]');
                        if (cb && !cb.checked) cb.click();
                    }""",
                    {"u": self.cfg["username"], "p": self.cfg["password"]},
                )
                code = self.read_captcha(
                    page,
                    "#yzm_img",
                    "() => { const i = document.getElementById('yzm_img'); if (i) i.src = '/secure/CheckCode.aspx?r=' + Math.random(); }",
                )
                if not code:
                    continue
                page.evaluate(
                    """(c) => { const e = document.getElementById('txt_img_code');
                               if (e) { e.value = c; e.dispatchEvent(new Event('input', {bubbles: true})); } }""",
                    code,
                )
                page.evaluate("""() => { const b = document.querySelector('.btn_login[data-login-type="1"]');
                                         if (b) b.click(); }""")
                time.sleep(4)
                if self.is_logged_in(page):
                    self.save_storage()
                    return True
            except Exception:
                time.sleep(2)
            time.sleep(random.uniform(1, 2))
        return False

    def ensure_login(self, page: Page) -> bool:
        page.goto(f"{self.base}/pages/cme.aspx", wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        if self.is_logged_in(page):
            return True
        return self.login(page)

    # ---------- 页面级异常验证码 ----------
    def solve_inline_captcha(self, page: Page) -> bool:
        """部分页面（考试反作弊）会弹「检测到考试异常」，需要填 5 位验证码。"""
        try:
            page.locator("#imgCheckCode").wait_for(state="visible", timeout=5000)
        except Exception:
            return False
        code = self.read_captcha(page, "#imgCheckCode")
        if not code:
            return False
        page.evaluate(
            """(x) => { const e = document.getElementById('txtCheckCode');
                        if (e) { e.value = x; e.dispatchEvent(new Event('input', {bubbles: true})); } }""",
            code,
        )
        page.evaluate("() => { const b = document.getElementById('btnYes'); if (b) b.click(); }")
        time.sleep(3)
        return True
