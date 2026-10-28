"""
天翼云盘扫码登录

做法：后台开一个无头 Chromium 打开官方登录页，把里面的二维码取出来给前端显示。
用户用手机「天翼云盘 App / 小翼管家 / 支付宝」扫一下确认，程序检测到登录 cookie 就收工。
不碰密码，也不逆向登录协议。
"""
from __future__ import annotations

import base64
import os
import threading
import time
import traceback

LOGIN_URL = "https://cloud.189.cn/web/login.html"
COOKIE_NAME = "COOKIE_LOGIN_USER"

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/snap/bin/chromium",
]


def _find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


class QRLoginManager:
    """一次只维护一个扫码会话"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"       # idle | starting | waiting | scanned | success | expired | error
        self.message = ""
        self.qr_png: bytes | None = None
        self.qr_at = 0.0
        self.cookie = ""
        self.account = ""

    # ---------- 对外接口 ----------

    def snapshot(self) -> dict:
        with self._lock:
            img = ""
            if self.qr_png:
                img = "data:image/png;base64," + base64.b64encode(self.qr_png).decode()
            return {
                "state": self.state,
                "message": self.message,
                "qr": img,
                "qr_age": round(time.time() - self.qr_at, 1) if self.qr_at else 0,
                "account": self.account,
            }

    def start(self, force: bool = False):
        with self._lock:
            if self._thread and self._thread.is_alive() and not force:
                return
            if self._thread and self._thread.is_alive():
                self._stop.set()
                self._thread.join(timeout=8)
            self._stop.clear()
            self.state = "starting"
            self.message = "正在启动浏览器…"
            self.qr_png = None
            self.qr_at = 0.0
            self.cookie = ""
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=8)
            self.state = "idle"
            self.qr_png = None

    # ---------- 内部 ----------

    def _set(self, state: str, message: str = ""):
        self.state = state
        self.message = message

    def _grab_qr(self, ctx, frame) -> bytes | None:
        # 优先拿原始二维码图片
        try:
            img = frame.locator("#j-qrcodeImage")
            img.wait_for(state="visible", timeout=15000)
            src = img.get_attribute("src") or ""
            if src.startswith("http"):
                r = ctx.request.get(src, timeout=15000)
                if r.ok and len(r.body()) > 300:
                    return r.body()
            return img.screenshot(timeout=8000)
        except Exception:
            return None

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self._set("error", f"浏览器组件没装好：{e}")
            return

        chrome = _find_chrome()
        try:
            with sync_playwright() as pw:
                launch_args = [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--window-size=1280,900",
                ]
                kwargs = {"headless": True, "args": launch_args}
                if chrome:
                    kwargs["executable_path"] = chrome
                browser = pw.chromium.launch(**kwargs)

                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                page = ctx.new_page()
                page.set_default_timeout(30000)

                last_refresh = 0.0
                while not self._stop.is_set():
                    try:
                        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                    except Exception as e:
                        self._set("error", f"打不开登录页：{e}")
                        time.sleep(3)
                        continue

                    # 等二维码出现
                    frame = None
                    for _ in range(25):
                        if self._stop.is_set():
                            break
                        for f in page.frames:
                            if "open.e.189.cn" in (f.url or ""):
                                frame = f
                                break
                        if frame:
                            break
                        page.wait_for_timeout(600)

                    png = self._grab_qr(ctx, frame) if frame else None
                    if png:
                        with self._lock:
                            self.qr_png = png
                            self.qr_at = time.time()
                        self._set("waiting", "用手机扫码")
                    else:
                        self._set("error", "没取到二维码，点重新生成试试")

                    last_refresh = time.time()

                    # 轮询登录状态
                    while not self._stop.is_set():
                        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                        token = cookies.get(COOKIE_NAME, "")
                        if token:
                            self.cookie = token
                            self._set("success", "登录成功")
                            self._persist(cookies)
                            browser.close()
                            return

                        if not self.qr_png:
                            break

                        # 二维码 100 秒刷新一次（官方有效期约 2 分钟）
                        if time.time() - last_refresh > 100:
                            self._set("waiting", "二维码已刷新")
                            break

                        # 扫码后页面会提示"扫描成功"
                        try:
                            if frame:
                                txt = frame.locator("body").inner_text(timeout=2000)
                                if "扫描成功" in txt or "请在手机" in txt:
                                    self._set("scanned", "已扫描，请在手机上点确认")
                        except Exception:
                            pass

                        page.wait_for_timeout(1500)

                    if self._stop.is_set():
                        break

                browser.close()
        except Exception as e:
            self._set("error", f"扫码出错：{e}")
            traceback.print_exc()
        finally:
            if self.state not in ("success",):
                with self._lock:
                    if self.state not in ("error", "success"):
                        self.state = "idle"

    def _persist(self, cookies: dict):
        try:
            from . import store

            store.save_cookie(cookies.get(COOKIE_NAME, ""))
        except Exception:
            pass


manager = QRLoginManager()
