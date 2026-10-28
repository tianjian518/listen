"""Listen · 云端音乐转存面板 (FastAPI)"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import media, probe, qrlogin, store
from core.media import COOKIE_FILE, clear_yt_cookie, has_yt_cookie, save_yt_cookie
from core.tasks import TaskManager
from core.tianyi import TianyiClient, TianyiError

BASE = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("MUSIC_PANEL_DOWNLOADS", BASE / "downloads"))

app = FastAPI(title="Listen", docs_url=None, redoc_url=None)
tasks = TaskManager(DOWNLOAD_DIR)


@app.on_event("startup")
def _startup():
    probe.start()  # 启动即自动体检所有音源

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

_THUMB_CACHE: dict[str, bytes] = {}


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return FileResponse(str(BASE / "static" / "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------- 云盘状态 ----------------

def _probe_cloud() -> dict:
    cfg = store.load()
    if not cfg.get("cookie"):
        return {"logged_in": False, "reason": "未登录"}
    try:
        client = TianyiClient(cfg["cookie"])
        info = client.space_info()
        client.close()
        return info
    except TianyiError as e:
        return {"logged_in": False, "reason": str(e)}
    except Exception as e:
        return {"logged_in": False, "reason": f"连接异常：{e}"}


@app.get("/api/status")
def status():
    cfg = store.load()
    info = _probe_cloud()
    info["folder_id"] = cfg.get("folder_id", "-11")
    info["folder_name"] = cfg.get("folder_name", "我的音乐")
    info["auto_create"] = cfg.get("auto_create", True)
    info["download_dir"] = str(DOWNLOAD_DIR)
    info["yt_cookie"] = has_yt_cookie()
    try:
        info["disk_free"] = round(media.disk_free_gb(), 1)
    except Exception:
        info["disk_free"] = 0
    return info


# ---------------- 音源状态 ----------------

@app.get("/api/sources")
def api_sources():
    snap = probe.snapshot()
    return {
        "updated": snap["updated"],
        "probing": snap["probing"],
        "items": snap["sources"],
        "yt_cookie": has_yt_cookie(),
    }


@app.post("/api/probe")
def api_probe_now():
    probe.probe_all(background=True)
    return {"ok": True}


# ---------------- 自定义公益镜像 ----------------

class InvHostBody(BaseModel):
    host: str


@app.get("/api/invhosts")
def api_invhosts():
    return {"hosts": store.get_custom_hosts()}


@app.post("/api/invhosts")
def api_invhosts_add(body: InvHostBody):
    hosts = store.get_custom_hosts()
    hosts.append(body.host)
    saved = store.save_custom_hosts(hosts)
    probe.probe_all(background=True)  # 立即复检
    return {"ok": True, "hosts": saved}


@app.delete("/api/invhosts/{host}")
def api_invhosts_del(host: str):
    saved = store.save_custom_hosts([h for h in store.get_custom_hosts() if h != host])
    return {"ok": True, "hosts": saved}


# ---------------- 音源 cookies ----------------

@app.get("/api/ytcookie")
def yt_cookie_status():
    return {"has": has_yt_cookie()}


@app.post("/api/ytcookie")
async def yt_cookie_upload(file: UploadFile | None = File(None)):
    if not file:
        raise HTTPException(400, "没有收到文件")
    content = await file.read()
    if not save_yt_cookie(content):
        raise HTTPException(400, "文件内容不对：应该是从 YouTube 导出的 cookies.txt")
    return {"ok": True, "size": len(content)}


@app.delete("/api/ytcookie")
def yt_cookie_delete():
    clear_yt_cookie()
    return {"ok": True}


@app.post("/api/logout")
def logout():
    store.clear_cookie()
    return {"ok": True}


# ---------------- 扫码登录 ----------------

@app.post("/api/qr/start")
def qr_start(force: bool = False):
    qrlogin.manager.start(force=force)
    return qrlogin.manager.snapshot()


@app.get("/api/qr/state")
def qr_state():
    snap = qrlogin.manager.snapshot()
    if snap["state"] == "success" and snap["qr"] == "":
        pass
    return snap


@app.post("/api/qr/cancel")
def qr_cancel():
    qrlogin.manager.stop()
    return {"ok": True}


# ---------------- 搜索 ----------------

class SearchBody(BaseModel):
    q: str
    limit: int = 18


@app.post("/api/search")
def api_search(body: SearchBody):
    kw = (body.q or "").strip()
    if len(kw) < 1:
        raise HTTPException(400, "输入点什么吧")
    try:
        items = media.search(kw, body.limit)
    except Exception as e:
        raise HTTPException(502, f"搜索失败：{e}")
    return {"items": items, "keyword": kw}


@app.get("/api/thumb/{vid}")
def thumb(vid: str):
    if vid in _THUMB_CACHE:
        return Response(_THUMB_CACHE[vid], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    try:
        r = httpx.get(url, timeout=12, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 500:
            _THUMB_CACHE[vid] = r.content
            if len(_THUMB_CACHE) > 500:
                _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
            return Response(r.content, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass
    return Response(status_code=404)


# ---------------- 上传目录 ----------------

@app.get("/api/folders")
def api_folders(parent: str = "-11"):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "还没登录天翼云盘")
    try:
        client = TianyiClient(cfg["cookie"])
        client.connect()
        data = client.list_folders(parent)
        client.close()
        return data
    except TianyiError as e:
        raise HTTPException(400, str(e))


class FolderBody(BaseModel):
    id: str | None = None
    name: str | None = None
    auto_create: bool | None = None


@app.post("/api/folder")
def api_set_folder(body: FolderBody):
    patch = {}
    if body.id is not None:
        patch["folder_id"] = body.id
    if body.name is not None:
        patch["folder_name"] = body.name
    if body.auto_create is not None:
        patch["auto_create"] = body.auto_create
    store.save(**patch)
    return {"ok": True, **store.load()}


# ---------------- 转存 ----------------

class TransferBody(BaseModel):
    video_id: str
    title: str = ""
    source: str = "invidious"
    host: str = ""


@app.post("/api/transfer")
def api_transfer(body: TransferBody):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    if not body.video_id:
        raise HTTPException(400, "缺少视频 ID")
    tid = tasks.submit(body.video_id, body.title or body.video_id,
                       source=body.source, host=body.host)
    return {"task_id": tid}


@app.get("/api/tasks")
def api_tasks():
    return {"items": tasks.list()}


@app.post("/api/tasks/{tid}/retry")
def api_retry(tid: str):
    new_id = tasks.retry(tid)
    if not new_id:
        raise HTTPException(400, "这条任务不能重试")
    return {"task_id": new_id}


@app.exception_handler(TianyiError)
def _tianyi_err(request: Request, exc: TianyiError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------- 音乐库（播放器） ----------------

AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac")


def _resolve_folder(client: TianyiClient, cfg: dict) -> str:
    folder_id = str(cfg.get("folder_id") or "-11")
    if folder_id == "-11" and cfg.get("auto_create", True):
        try:
            return client.ensure_folder((cfg.get("folder_name") or "我的音乐").strip("/"), "-11")
        except Exception:
            return "-11"
    return folder_id


@app.get("/api/library")
def api_library():
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    try:
        client = TianyiClient(cfg["cookie"])
        client.connect()
        folder_id = _resolve_folder(client, cfg)
        data = client.list_folders(folder_id)
        client.close()
    except TianyiError as e:
        raise HTTPException(400, str(e))
    songs = [
        {
            "id": f["id"],
            "name": f["name"],
            "title": f["name"].rsplit(".", 1)[0],
            "size": f.get("size", 0),
        }
        for f in data["files"]
        if f["name"].lower().endswith(AUDIO_EXT)
    ]
    return {"songs": songs, "folder_name": cfg.get("folder_name", "我的音乐")}


@app.get("/api/play/{file_id}")
def api_play(file_id: str):
    """302 直链播放：浏览器/手机直连天翼 CDN，服务器零流量中转"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    try:
        client = TianyiClient(cfg["cookie"])
        client.connect()
        url = client.get_download_url(file_id)
        client.close()
    except TianyiError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url, status_code=302)
