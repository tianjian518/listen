"""
天翼云盘扫码登录（纯 HTTP 实现，不需要浏览器）

流程全部是 open.e.189.cn 上的公共接口：
  1. GET  cloud.189.cn/api/portal/loginUrl.action     → lt / reqId / appId
  2. POST open.e.189.cn/api/logbox/oauth2/appConf.do  → paramId / returnUrl / isOauth2
  3. POST .../oauth2/getUUID.do                       → uuid / encryuuid / encodeuuid
  4. GET  .../oauth2/image.do                         → 二维码 JPEG
  5. POST .../oauth2/qrcodeLoginState.do              → -106 等待 / -11002 已扫 / 0 成功
  6. GET  redirectUrl                                 → 拿到 COOKIE_LOGIN_USER

注意两点：
  * open.e.189.cn 会校验 TLS 指纹，普通 requests 会读超时，必须用 curl_cffi；
  * encodeuuid 本身已是 URL 编码过的串，拼进 image.do 时不能再让 HTTP 库编码一次，
    否则 %3D 会变成 %253D，手机扫出来是无效链接。
"""
from __future__ import annotations

import base64
import random
import threading
import time
import traceback
import urllib.parse

COOKIE_NAME = "COOKIE_LOGIN_USER"
PORTAL = "https://cloud.189.cn"
DOMAIN = "https://open.e.189.cn"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

QR_TTL = 230       # 单张二维码等多久（官方有效期约 4 分钟）
TOTAL_TTL = 3600   # 整个会话最长等多久
POLL_GAP = 1.8     # 轮询间隔


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S") + str(random.randint(10, 99))


def _ms() -> int:
    return int(time.time() * 1000)


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
            self.message = "正在获取二维码…"
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
        # 不拿锁：start() 持锁 join 线程时会被这里卡住；单字段赋值本身是原子操作
        self.state = state
        self.message = message

    def _session(self):
        from curl_cffi import requests as creq

        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        return s

    @staticmethod
    def _call(s, method: str, url: str, tries: int = 3, **kw):
        """带重试的请求，全失败返回 None"""
        for i in range(tries):
            try:
                r = s.get(url, timeout=20, **kw) if method == "GET" else s.post(url, timeout=20, **kw)
                if r.status_code == 200:
                    return r
            except Exception:
                pass
            if i < tries - 1:
                time.sleep(1.5)
        return None

    def _refresh_qr(self, s):
        """走一遍 1~4 步，出图并返回轮询所需参数；失败返回 None"""
        r = self._call(
            s, "GET", f"{PORTAL}/api/portal/loginUrl.action",
            params={"redirectURL": f"{PORTAL}/web/redirect.html"},
        )
        if r is None:
            return None

        q = urllib.parse.parse_qs(urllib.parse.urlparse(r.url).query)
        lt = q.get("lt", [""])[0]
        req_id = q.get("reqId", [""])[0]
        app_id = q.get("appId", ["cloud"])[0]
        if not req_id:
            return None

        h = {
            "lt": lt, "reqid": req_id, "reqId": req_id,
            "referer": r.url, "origin": DOMAIN,
            "Accept": "application/json;charset=UTF-8",
        }

        # 第 2 步：应用配置
        r = self._call(s, "POST", f"{DOMAIN}/api/logbox/oauth2/appConf.do",
                       data={"version": "2.0", "appKey": app_id}, headers=h)
        conf = (r.json().get("data") or {}) if r else {}

        # 第 3 步：拿 uuid
        r = self._call(s, "POST", f"{DOMAIN}/api/logbox/oauth2/getUUID.do",
                       data={"appId": app_id}, headers=h)
        u = r.json() if r else {}
        uuid = u.get("uuid", "")
        encryuuid = u.get("encryuuid", "")
        encodeuuid = u.get("encodeuuid", "")
        if not encryuuid:
            return None

        # 第 4 步：出图。encodeuuid 已编码，直接拼 URL，不能交给库再编码
        r = self._call(
            s, "GET",
            f"{DOMAIN}/api/logbox/oauth2/image.do?uuid={encodeuuid}&REQID={req_id}",
            headers=h,
        )
        if r is None or len(r.content) < 500:
            return None

        with self._lock:
            self.qr_png = r.content
            self.qr_at = time.time()
        self._set("waiting", "用手机扫码")

        return h, {
            "appId": app_id,
            "encryuuid": encryuuid,
            "uuid": uuid,
            "returnUrl": conf.get("returnUrl") or "",
            "clientType": "1",
            "cb_SaveName": "3",
            "isOauth2": str(conf.get("isOauth2", "false")).lower(),
            "state": conf.get("state") or "",
            "paramId": conf.get("paramId") or "",
        }

    def _poll(self, s, h, body) -> bool:
        """轮询扫码状态；成功返回 True，过期返回 False"""
        end = time.time() + QR_TTL
        last = None
        while time.time() < end and not self._stop.is_set():
            b = dict(body)
            b["date"] = _stamp()
            b["timeStamp"] = str(_ms())
            r = self._call(s, "POST", f"{DOMAIN}/api/logbox/oauth2/qrcodeLoginState.do",
                           data=b, headers=h, tries=1)
            if r is None:
                time.sleep(POLL_GAP)
                continue
            try:
                j = r.json()
            except Exception:
                time.sleep(POLL_GAP)
                continue

            st = j.get("status")
            if st != last:
                last = st
                if st == -11002:
                    self._set("scanned", "已扫描，请在手机上点确认")
                elif st == -106:
                    self._set("waiting", "用手机扫码")
                elif st in (-11001, -20099):
                    return False

            if st == 0:
                url = j.get("redirectUrl") or j.get("toUrl") or ""
                if url:
                    self._call(s, "GET", url)
                self._finish(s)
                return True

            time.sleep(POLL_GAP)
        return False

    def _finish(self, s):
        """登录成功：存 cookie、取账号名"""
        try:
            cookies = {c.name: c.value for c in s.cookies.jar}
        except AttributeError:
            cookies = s.cookies.get_dict()
        token = cookies.get(COOKIE_NAME, "")
        if not token:
            self._set("error", "登录成功但没拿到凭证，请重试")
            return

        self.cookie = token
        account = ""
        try:
            r = self._call(s, "GET", f"{PORTAL}/v2/getUserBriefInfo.action")
            if r is not None:
                d = r.json()
                account = d.get("userAccount", "") or d.get("nickname", "")
        except Exception:
            pass

        self._persist(token, account)
        with self._lock:
            self.account = account
        self._set("success", "登录成功")

    def _run(self):
        try:
            s = self._session()
        except Exception as e:
            self._set("error", f"缺少网络组件 curl_cffi：{e}")
            return

        deadline = time.time() + TOTAL_TTL
        try:
            while time.time() < deadline and not self._stop.is_set():
                got = self._refresh_qr(s)
                if not got:
                    self._set("error", "获取二维码失败，网络不稳定，请重试")
                    time.sleep(3)
                    continue
                h, body = got
                if self._poll(s, h, body):
                    return
                if self._stop.is_set():
                    break
                self._set("waiting", "二维码已过期，正在换新…")
                time.sleep(1)
            else:
                self._set("expired", "等太久了，点重新生成二维码")
        except Exception as e:
            self._set("error", f"扫码出错：{e}")
            traceback.print_exc()

    def _persist(self, cookie: str, account: str = ""):
        try:
            from . import store

            store.save(cookie=cookie, account=account)
        except Exception:
            traceback.print_exc()


manager = QRLoginManager()
