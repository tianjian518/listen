"""listen - FastAPI 主程序"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import audiobook, convert, media, probe, qrlogin, store
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

tasks = TaskManager(DOWNLOAD_DIR, BASE / "tasks.json")
tasks.on_finish = lambda _tid: _drop_lib_cache()


@app.on_event("startup")
def _startup():
    probe.start(BASE / "probe.json")  # 先读上次结果，再后台复检


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
                        headers={"Cache-Control": "public, max-age=2592000, immutable"})
    url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    try:
        r = httpx.get(url, timeout=12, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 500:
            _THUMB_CACHE[vid] = r.content
            if len(_THUMB_CACHE) > 500:
                _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
            return Response(r.content, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=2592000, immutable"})
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

class RenameBody(BaseModel):
    title: str = ""
    current_name: str = ""


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


@app.post("/api/tasks/retry-failed")
def api_retry_all_failed():
    """一键重试所有失败任务"""
    n = tasks.retry_all_failed()
    return {"ok": True, "count": n}


@app.post("/api/tasks/{tid}/retry")
def api_retry(tid: str):
    new_id = tasks.retry(tid)
    if not new_id:
        raise HTTPException(400, "这条任务不能重试")
    return {"task_id": new_id}


@app.delete("/api/tasks/{tid}")
def api_task_remove(tid: str):
    """删除单条记录（运行中的不能删）"""
    if not tasks.remove(tid):
        raise HTTPException(400, "这条任务还在进行中，不能删除")
    return {"ok": True}


@app.delete("/api/tasks")
def api_tasks_clear():
    n = tasks.clear_finished()
    return {"ok": True, "removed": n}


@app.exception_handler(TianyiError)
def _tianyi_err(request: Request, exc: TianyiError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------- 转码 ----------------
# 云端放不了的格式（wma / ape / 视频容器）在服务器上转成 m4a 再传回去。
# 串行跑，一次只占一个文件的磁盘；原文件默认保留。

def _cv_done(job: dict, cfg: dict):
    """转完一集，让书单和播放列表下次直接重新读"""
    _BOOK_CACHE.clear()
    _SHELF.update(key="", at=0.0, data=None)
    _LIB_CACHE.update(key="", at=0.0, data=None)


_CV = convert.Studio(
    path=BASE / "convert.json",
    tmpdir=DOWNLOAD_DIR / "_convert",
    client_factory=_shared_client,
    on_done=_cv_done,
)


class CvItem(BaseModel):
    id: str
    name: str = ""
    size: int = 0
    folder: str = ""
    book: str = ""


class CvStartBody(BaseModel):
    items: list[CvItem] = []


class CvIdsBody(BaseModel):
    ids: list[str] = []


class CvScanBody(BaseModel):
    root: str = ""


class CvCfgBody(BaseModel):
    format: str = ""
    bitrate: int = 0
    keep_origin: bool | None = None
    video: bool | None = None


@app.get("/api/convert/status")
def cv_status():
    cfg = store.load()
    data = _CV.status(cfg)
    data["logged_in"] = bool(cfg.get("cookie"))
    data["root"] = _book_root(cfg)
    return data


@app.post("/api/convert/scan")
def cv_scan(body: CvScanBody | None = None):
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    root = ((body.root if body else "") or _book_root(cfg)).strip().strip("/")
    return _CV.scan_now(root)


@app.post("/api/convert/start")
def cv_start(body: CvStartBody):
    if not store.load().get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    if not body.items:
        raise HTTPException(400, "先勾选要转的文件")
    n = _CV.enqueue([i.model_dump() for i in body.items])
    return {"ok": True, "added": n}


@app.post("/api/convert/stop")
def cv_stop():
    return _CV.stop()


@app.post("/api/convert/resume")
def cv_resume():
    return _CV.resume()


@app.post("/api/convert/retry")
def cv_retry(body: CvIdsBody):
    return {"ok": True, "n": _CV.retry(body.ids or None)}


@app.post("/api/convert/remove")
def cv_remove(body: CvIdsBody):
    return {"ok": True, "n": _CV.remove(body.ids)}


@app.post("/api/convert/clear")
def cv_clear():
    return {"ok": True, "n": _CV.clear_finished()}


@app.post("/api/convert/delete-origin")
def cv_delete_origin(body: CvIdsBody):
    if not body.ids:
        raise HTTPException(400, "没选文件")
    return _CV.delete_origin(body.ids)


@app.post("/api/convert/cfg")
def cv_cfg(body: CvCfgBody):
    kw = {}
    if body.format:
        fmt = body.format.lower()
        if fmt not in ("m4a", "mp3"):
            raise HTTPException(400, "只支持 m4a / mp3")
        kw["convert_format"] = fmt
    if body.bitrate:
        if not 32 <= body.bitrate <= 320:
            raise HTTPException(400, "码率填 32~320 之间")
        kw["convert_bitrate"] = body.bitrate
    if body.keep_origin is not None:
        kw["convert_keep_origin"] = bool(body.keep_origin)
    if body.video is not None:
        kw["convert_video"] = bool(body.video)
    if kw:
        store.save(**kw)
    return {"ok": True}


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


# ============================ 听书 ============================
# 书架只列书的文件夹（1 次请求，秒开）；点进某本书才扫章节（带缓存）。

_PROGRESS_FILE = Path(os.environ.get("MUSIC_PANEL_PROGRESS", BASE / "progress.json"))
_progress = audiobook.Progress(_PROGRESS_FILE)

_SHELF: dict = {"key": "", "at": 0.0, "data": None}
_SHELF_TTL = 60.0
_BOOK_CACHE: dict[str, tuple[float, dict]] = {}
_BOOK_TTL = 600.0
_SCAN_LOCK = threading.Lock()

# 书架上的「共多少集」。数一次的代价是每本书 1 个请求（有卷的每卷再 1 个），
# 21 本书并发 6 大概 3 秒，所以存在内存 + counts.json 里，TTL 半小时。
_COUNTS_FILE = Path(os.environ.get("MUSIC_PANEL_COUNTS", BASE / "counts.json"))
_COUNTS: dict[str, tuple[float, int]] = {}
_COUNTS_TTL = 1800.0
_COUNT_GUARD = threading.Lock()
# on: 是否正在数；since: 这一轮开始的时间；done: 这一轮已经数出几本
_COUNT_RUN = {"on": False, "since": 0.0, "done": 0}
_COUNT_IDLE = 150.0        # 一轮超过这么久还没结束，就不再等它，重新起一轮


def _load_counts():
    try:
        d = json.loads(_COUNTS_FILE.read_text("utf-8"))
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, list) and len(v) == 2:
                    _COUNTS[str(k)] = (float(v[0]), int(v[1]))
    except Exception:
        pass


def _save_counts():
    try:
        with _COUNT_GUARD:
            out = {k: [v[0], v[1]] for k, v in _COUNTS.items()}
        tmp = _COUNTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
        tmp.replace(_COUNTS_FILE)
    except Exception:
        pass


def _apply_counts(books: list):
    """把已知的集数贴到书架上，不知道的留空"""
    now = time.time()
    for b in books:
        if b.get("single"):
            continue
        hit = _COUNTS.get(b["id"])
        if hit and now - hit[0] < _COUNTS_TTL:
            b["count"] = hit[1]
            continue
        bk = _BOOK_CACHE.get(b["id"])
        if bk and bk[1].get("count"):
            b["count"] = int(bk[1]["count"])


def _count_worker(ids: list):
    """后台把没数过的书数一遍：并发 3，数一本记一本，随时可查进度"""
    cfg = store.load()
    cli = TianyiClient(cfg.get("cookie") or "")
    try:
        cli.connect()
    except Exception:
        with _COUNT_GUARD:
            _COUNT_RUN["on"] = False
        return

    def one(bid):
        for attempt in range(2):                     # 失败重试一次，天翼偶发抽风
            try:
                n = audiobook.count_book(cli, bid)
                if n:
                    return bid, n
                time.sleep(0.4)
            except Exception:
                time.sleep(0.6)
        return bid, 0

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(one, bid) for bid in ids]
            for ft in as_completed(futs):
                try:
                    bid, n = ft.result()
                except Exception:
                    continue
                if n:
                    with _COUNT_GUARD:
                        _COUNTS[bid] = (time.time(), int(n))
                        _COUNT_RUN["done"] += 1
    finally:
        try:
            cli.close()
        except Exception:
            pass
        _save_counts()
        with _COUNT_GUARD:
            _COUNT_RUN["on"] = False


def _kick_counts(books: list) -> bool:
    """还有书没数过集数就在后台数，返回是否还在数"""
    now = time.time()
    with _COUNT_GUARD:
        miss = [b["id"] for b in books
                if not b.get("single") and b.get("id") and not b.get("count")]
        if not miss:
            return False
        running = _COUNT_RUN["on"] and (now - _COUNT_RUN["since"] < _COUNT_IDLE)
        if running:
            return True
        _COUNT_RUN.update(on=True, since=now, done=0)
    threading.Thread(target=_count_worker, args=(miss,), daemon=True).start()
    return True


_load_counts()


def _book_root(cfg: dict) -> str:
    return ((cfg.get("book_folder") or "有声书").strip().strip("/")) or "有声书"


def _merge_progress(books: list) -> list:
    prog = _progress.all()
    for b in books:
        p = prog.get(b["id"]) or {}
        b["heard"] = p.get("index", -1)          # 听到第几集（0 基）
        b["heard_count"] = p.get("count", 0)      # 总集数（听到时记录的）
        b["pos"] = p.get("pos", 0)
        b["updated"] = p.get("updated", 0)
    return books


class BookFolderBody(BaseModel):
    name: str = ""


class BookNameBody(BaseModel):
    order: str = "auto"


class ProgressBody(BaseModel):
    book_id: str
    index: int = 0
    file_id: str = ""
    pos: float = 0
    dur: float = 0
    count: int = 0


@app.get("/api/books")
def api_books(fresh: int = 0):
    """书架：只列书，不进去数集数"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    root = _book_root(cfg)
    now = time.time()
    if (not fresh and _SHELF["data"] is not None and _SHELF["key"] == root
            and now - _SHELF["at"] < _SHELF_TTL):
        data = _SHELF["data"]
    else:
        try:
            data = audiobook.scan_shelf(_shared_client(), root,
                                        cfg.get("book_name_order") or "auto")
        except TianyiError as e:
            raise HTTPException(400, str(e))
        _merge_progress(data["books"])
        _SHELF.update(key=root, at=now, data=data)

    _apply_counts(data["books"])
    data["counting"] = _kick_counts(data["books"])   # 还没数完的，前端过几秒自己来拿
    return data


@app.get("/api/books/{folder_id}")
def api_book_chapters(folder_id: str, single: int = 0, fresh: int = 0):
    """某本书的章节列表（含分卷），带缓存"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    now = time.time()
    hit = _BOOK_CACHE.get(folder_id)
    if not fresh and hit and now - hit[0] < _BOOK_TTL:
        data = hit[1]
    else:
        try:
            with _SCAN_LOCK:
                data = audiobook.scan_book(_shared_client(), folder_id,
                                           as_single=bool(single))
        except TianyiError as e:
            raise HTTPException(400, str(e))
        _BOOK_CACHE[folder_id] = (now, data)
        if data.get("count"):
            # 点开过一本书，书架上那本顺便就有集数了
            with _COUNT_GUARD:
                _COUNTS[folder_id] = (now, int(data["count"]))
    out = dict(data)
    out["progress"] = _progress.get(folder_id)
    return out


@app.get("/api/bookfolder")
def api_book_folder_get():
    return {"name": _book_root(store.load())}


@app.post("/api/bookfolder")
def api_book_folder_set(body: BookFolderBody):
    name = (body.name or "").strip().strip("/")
    if not name:
        raise HTTPException(400, "目录名不能为空")
    if "/" in name or "\\" in name:
        raise HTTPException(400, "只填云盘根目录下的一级文件夹名，不要带斜杠")
    store.save(book_folder=name)
    _SHELF.update(key="", at=0.0, data=None)
    _BOOK_CACHE.clear()
    return {"ok": True, "name": name}


@app.get("/api/bookname")
def api_book_name_get():
    return {"order": store.load().get("book_name_order") or "auto"}


@app.post("/api/bookname")
def api_book_name_set(body: BookNameBody):
    """文件夹名的命名习惯：auto（自动认）/ title（书名在前）/ author（作者在前）"""
    order = (body.order or "auto").strip()
    if order not in ("auto", "title", "author"):
        raise HTTPException(400, "只能是 auto / title / author")
    store.save(book_name_order=order)
    _SHELF.update(key="", at=0.0, data=None)
    return {"ok": True, "order": order}


@app.get("/api/progress")
def api_progress_all():
    return {"items": _progress.all()}


@app.post("/api/progress")
def api_progress_save(body: ProgressBody):
    if not body.book_id:
        raise HTTPException(400, "缺少 book_id")
    return _progress.set(
        body.book_id,
        index=max(0, int(body.index)),
        file_id=body.file_id or "",
        pos=round(float(body.pos or 0), 1),
        dur=round(float(body.dur or 0), 1),
        count=max(0, int(body.count)),
    )


@app.delete("/api/progress/{book_id}")
def api_progress_clear(book_id: str):
    return {"ok": _progress.clear(book_id)}


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


@app.post("/api/library/{file_id}/rename")
def api_rename_song(file_id: str, body: RenameBody):
    """给云盘里已有的歌改名，同步到天翼云盘"""
    cfg = store.load()
    if not cfg.get("cookie"):
        raise HTTPException(401, "请先扫码登录天翼云盘")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "名字不能为空")
    if len(title) > 100:
        raise HTTPException(400, "名字太长（最多 100 字）")
    # 去掉文件名里不允许的字符
    for ch in '\\/:*?"<>|':
        title = title.replace(ch, "")
    title = title.strip()
    if not title:
        raise HTTPException(400, "名字里没有可用字符")

    # 保留原扩展名，否则播放器可能不认
    ext = Path(body.current_name or "").suffix
    if not ext or len(ext) > 6:
        ext = ".mp3"
    final_name = title + ext

    client = _shared_client()
    client.rename(file_id, final_name)
    _drop_lib_cache()
    return {"ok": True, "name": final_name, "title": title}


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
