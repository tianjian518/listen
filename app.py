"""listen - FastAPI 主程序"""
from __future__ import annotations

import os
import time
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

app = FastAPI(title="listen", docs_url=None, redoc_url=None)

# ---------- 云盘客户端复用 + 列表缓存 ----------
# 天翼每次 connect() 要换一次 accessToken，列表页频繁刷新会白等好几秒，
# 所以进程内复用同一个客户端，并给文件列表加短缓存（转存/删除后主动失效）。

_LIB_CACHE = {"key": "", "at": 0.0, "data": None}
_LIB_TTL = 20.0


def _shared_client():
    """复用一个已连接的客户端；cookie 变了就重建"""
    cfg = store.load()
    cookie = cfg.get("cookie") or ""
    if not cookie:
        raise HTTPException(401, "请先扫码登录天翼云盘")
    cli = getattr(app.state, "tianyi", None)
    if cli is not None and getattr(cli, "cookie", "") == cookie:
        try:
            if getattr(cli, "session_key", "") and getattr(cli, "access_token", ""):
                return cli
        except Exception:
            pass
    if cli is not None:
        try:
            cli.close()
        except Exception:
            pass
    cli = TianyiClient(cookie)
    cli.connect()
    app.state.tianyi = cli
    return cli


def _drop_lib_cache():
    _LIB_CACHE["key"] = ""
    _LIB_CACHE["at"] = 0.0
    _LIB_CACHE["data"] = None

tasks = TaskManager(DOWNLOAD_DIR)
tasks.on_finish = lambda _tid: _drop_lib_cache()


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
    client = None
    try:
        client = TianyiClient(cfg["cookie"])
        return client.space_info()
    except TianyiError as e:
        return {"logged_in": False, "reason": str(e)}
    except Exception as e:
        return {"logged_in": False, "reason": f"连接异常：{e}"}
    finally:
        if client:
            client.close()


@app.get("/api/status")
def status():
    cfg = store.load()
    info = _probe_cloud()
    info["folder_id"] = cfg.get("folder_id", "-11")
    info["folder_name"] = cfg.get("folder_name", "我的音乐")
    info["auto_create"] = cfg.get("auto_create", True)
    info["quality"] = str(cfg.get("quality", "320"))
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
    return qrlogin.manager.snapshot()


@app.post("/api/qr/cancel")
def qr_cancel():
    qrlogin.manager.stop()
    return {"ok": True}


# ---------------- 搜索 ----------------

class SearchBody(BaseModel):
    q: str
    limit: int = 18


_HISTORY_KEY = "search_history"
_HISTORY_MAX = 12


def _push_history(keyword: str):
    try:
        hist = [h for h in (store.load().get(_HISTORY_KEY) or []) if h and h != keyword]
        hist.insert(0, keyword)
        store.save(**{_HISTORY_KEY: hist[:_HISTORY_MAX]})
    except Exception:
        pass


@app.get("/api/history")
def api_history():
    return {"items": store.load().get(_HISTORY_KEY) or []}


@app.delete("/api/history")
def api_history_clear():
    store.save(**{_HISTORY_KEY: []})
    return {"ok": True}


@app.post("/api/search")
def api_search(body: SearchBody):
    kw = (body.q or "").strip()
    if len(kw) < 1:
        raise HTTPException(400, "输入点什么吧")
    try:
        items = media.search(kw, body.limit)
    except Exception as e:
        raise HTTPException(502, f"搜索失败：{e}")
    if items:
        _push_history(kw)
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


# ---------------- 封面代理（聚合源的封面带防盗链，由服务器转一道） ----------------

@app.get("/api/cover")
def cover(u: str = ""):
    """把第三方封面图代理出来，避免浏览器跨域 / 防盗链导致不显示"""
    if not u.startswith("http"):
        raise HTTPException(400, "bad url")
    if not any(d in u for d in ("126.net", "gtimg.cn", "qq.com", "ytimg.com", "hdslb.com")):
        raise HTTPException(400, "不支持的封面来源")
    try:
        return _image_response(u, headers={"Referer": "https://music.163.com/",
                                           "User-Agent": "Mozilla/5.0"})
    except Exception:
        return Response(status_code=404)


_GDCOVER_CACHE: dict[str, tuple[bytes, str]] = {}


def _netease_pic_url(song_id: str) -> str:
    """调网易官方接口拿完整封面地址（pic_id 拼不出来，必须走这个接口）"""
    r = httpx.get(
        "https://music.163.com/api/song/detail",
        params={"ids": f"[{song_id}]"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
        timeout=10,
        follow_redirects=True,
    )
    if r.status_code != 200:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""
    songs = (data or {}).get("songs") or []
    if not songs:
        return ""
    song = songs[0]
    # 优先专辑封面，专辑没有就用歌手头像
    album = song.get("album") or {}
    for cand in (album.get("picUrl"), (song.get("artists") or [{}])[0].get("picUrl")):
        if isinstance(cand, str) and cand.startswith("http"):
            return cand
    return ""


@app.get("/api/gdcover")
def gdcover(source: str = "netease", id: str = "", album: str = ""):
    """聚合音源封面代理，取到后缓存在内存里，避免每次搜索都打接口"""
    key = f"{source}:{id or album}"
    hit = _GDCOVER_CACHE.get(key)
    if hit:
        return Response(hit[0], media_type=hit[1],
                        headers={"Cache-Control": "public, max-age=604800"})

    url = ""
    if source == "netease" and id:
        try:
            url = _netease_pic_url(id)
        except Exception:
            url = ""
    elif source == "tencent" and album and album.isdigit():
        url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album}_1.jpg"

    if not url:
        return Response(status_code=404)

    try:
        r = httpx.get(url + "?param=200y200", timeout=10, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
        body = r.content or b""
        ctype = r.headers.get("content-type", "image/jpeg")
        if r.status_code == 200 and len(body) > 1000 and "image" in ctype:
            if len(_GDCOVER_CACHE) > 400:
                _GDCOVER_CACHE.pop(next(iter(_GDCOVER_CACHE)))
            _GDCOVER_CACHE[key] = (body, ctype)
            return Response(body, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        pass
    return Response(status_code=404)


def _image_response(url: str, headers: dict | None = None, timeout: int = 12) -> Response:
    """下载一张图并原样返回；内容不是图片直接当失败处理"""
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    h.update(headers or {})
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=h)
    body = r.content or b""
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or len(body) < 500 or "image" not in ctype:
        return Response(status_code=404)
    return Response(body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=604800"})


# ---------------- 上传目录 ----------------

@app.get("/api/folders")
def api_folders(parent: str = "-11"):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "还没登录天翼云盘")
    client = None
    try:
        client = TianyiClient(cfg["cookie"])
        client.connect()
        return client.list_folders(parent)
    except TianyiError as e:
        raise HTTPException(400, str(e))
    finally:
        if client:
            client.close()


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


# ---------------- 音质设置 ----------------

class QualityBody(BaseModel):
    quality: str = "320"


@app.post("/api/quality")
def api_quality(body: QualityBody):
    q = str(body.quality)
    if q not in ("320", "192", "128"):
        raise HTTPException(400, "音质只能是 320 / 192 / 128")
    store.save(quality=q)
    return {"ok": True, "quality": q}


# ---------------- 转存 ----------------

class TransferBody(BaseModel):
    video_id: str
    title: str = ""
    source: str = "invidious"
    host: str = ""
    gid: str = ""
    cover: str = ""


@app.post("/api/transfer")
def api_transfer(body: TransferBody):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    if not body.video_id:
        raise HTTPException(400, "缺少视频 ID")
    tid = tasks.submit(body.video_id, body.title or body.video_id,
                       source=body.source, host=body.host, gid=body.gid,
                       cover=body.cover or "")
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


@app.delete("/api/tasks")
def api_tasks_clear():
    n = tasks.clear_finished()
    return {"ok": True, "removed": n}


@app.exception_handler(TianyiError)
def _tianyi_err(request: Request, exc: TianyiError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------- 音乐库（播放器） ----------------

AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac")

def _cover_memo() -> dict:
    """歌名 -> 封面地址，转存时记下来给音乐库兜底显示

    每次从配置里现读，避免进程内缓存和磁盘上的记录对不上。
    """
    try:
        m = store.load().get("cover_memo") or {}
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _resolve_folder(client: TianyiClient, cfg: dict) -> str:
    folder_id = str(cfg.get("folder_id") or "-11")
    if folder_id == "-11" and cfg.get("auto_create", True):
        try:
            return client.ensure_folder((cfg.get("folder_name") or "我的音乐").strip("/"), "-11")
        except Exception:
            return "-11"
    return folder_id


def _cover_from_icon(icon: dict | None) -> str:
    """天翼的文件 icon 里带封面地址，优先用小图"""
    if not isinstance(icon, dict):
        return ""
    for k in ("smallUrl", "mediumUrl", "largeUrl", "maxUrl", "url"):
        v = icon.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


@app.get("/api/library")
def api_library(fresh: int = 0):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    folder_id = str(cfg.get("folder_id") or "-11")
    cache_key = f"{cfg['cookie'][:8]}|{folder_id}"
    now = time.time()
    # 20 秒内重复进页面直接给缓存，播放/删除/转存会主动清掉
    if (not fresh and _LIB_CACHE["data"] is not None
            and _LIB_CACHE["key"] == cache_key
            and now - _LIB_CACHE["at"] < _LIB_TTL):
        return _LIB_CACHE["data"]
    try:
        client = _shared_client()
        folder_id = _resolve_folder(client, cfg)
        data = client.list_folders(folder_id)
    except TianyiError as e:
        _drop_lib_cache()
        raise HTTPException(400, str(e))
    memo = _cover_memo()
    songs = []
    for f in data["files"]:
        if not f["name"].lower().endswith(AUDIO_EXT):
            continue
        title = f["name"].rsplit(".", 1)[0]
        cover = f.get("cover", "")
        if not cover:
            # 云盘不给音乐文件返回封面，用之前转存时记下的封面兜底
            cover = memo.get(title, "")
        songs.append({
            "id": f["id"],
            "name": f["name"],
            "title": title,
            "size": f.get("size", 0),
            "cover": cover,
        })
    result = {
        "songs": songs,
        "folder_name": cfg.get("folder_name", "我的音乐"),
        "folder_id": folder_id,
    }
    _LIB_CACHE.update(key=cache_key, at=time.time(), data=result)
    return result


@app.get("/api/play/{file_id}")
def api_play(file_id: str):
    """302 直链播放：浏览器/手机直连天翼 CDN，服务器零流量中转"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    try:
        url = _shared_client().get_download_url(file_id)
    except TianyiError as e:
        _drop_lib_cache()
        raise HTTPException(400, str(e))
    return RedirectResponse(url, status_code=302)


@app.delete("/api/library/{file_id}")
def api_delete_song(file_id: str):
    """从云盘删歌（播放器里长按/点删除按钮触发）"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    try:
        _shared_client().delete_files([file_id])
    except TianyiError as e:
        raise HTTPException(400, str(e))
    _drop_lib_cache()
    return {"ok": True}
