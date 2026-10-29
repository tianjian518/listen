"""转码：把云盘里浏览器放不了的音频转成 m4a/mp3，再传回原目录

流程（串行，一次只处理一个文件）：
    拿直链 → 下载到本地临时目录 → ffmpeg 转码 → 上传回原目录 → 删掉临时文件

磁盘峰值只占「一个文件」的大小，几百集的书也不会把盘塞满。
原文件默认保留，转完在页面上标出来，确认没问题再手动清理。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import audiobook
from .tianyi import TianyiError

# 浏览器解不了的音频格式（Chrome / Safari / 安卓系统都不带这些解码器）
BAD_AUDIO_EXT = (".wma", ".ape", ".amr", ".mid", ".midi", ".aif", ".aiff",
                 ".dsf", ".dff", ".mka", ".ra", ".wv", ".tta")
# 视频容器：只抽第一条音轨，画面丢掉
VIDEO_EXT = (".mp4", ".mkv", ".flv", ".avi", ".mov", ".wmv", ".ts", ".m4v",
             ".webm", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp")

OUT_EXT = {"m4a": ".m4a", "mp3": ".mp3"}
# 已经是这些格式的，说明同目录已有可播版本
PLAYABLE_EXT = (".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wav")


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


class FlowOut(TianyiError):
    """天翼的上传流量用尽（data flow is out）。这时后面每一集都会失败，
    所以直接暂停整个队列，别一集一集白撞。"""


def _is_flow_out(e: Exception) -> bool:
    s = str(e).lower()
    return "data flow is out" in s or "flow is out" in s


def _fmt_err(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    return msg[:180]


class Studio:
    """转码队列 + 扫描器（都是后台线程，不阻塞网页）"""

    def __init__(self, path, tmpdir, client_factory, on_done=None):
        self.path = Path(path)
        self.tmpdir = Path(tmpdir)
        self._client = client_factory
        self._on_done = on_done
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._stop_flag = False
        self._scan = {"state": "idle", "root": "", "books": [], "msg": "",
                      "done": 0, "total": 0, "at": 0.0, "err": ""}
        self._scan_thread: threading.Thread | None = None
        self._paused = ""
        self._load()

    # ---------------- 落盘 ----------------

    def _load(self):
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text("utf-8"))
            jobs = d.get("jobs") if isinstance(d, dict) else None
            if isinstance(jobs, dict):
                for k, v in jobs.items():
                    if not isinstance(v, dict):
                        continue
                    if v.get("state") == "run":       # 上次没跑完 → 重新排队
                        v["state"] = "wait"
                        v["stage"] = ""
                        v["pct"] = 0
                        v["msg"] = "服务重启，重新排队"
                    self._jobs[k] = v
                    self._seq = max(self._seq, int(v.get("seq") or 0))
        except Exception:
            self._jobs = {}

    def _flush(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"jobs": self._jobs}, ensure_ascii=False),
                               "utf-8")
                tmp.replace(self.path)
            except Exception:
                pass

    # ---------------- 对外：状态 ----------------

    def status(self, cfg: dict) -> dict:
        with self._lock:
            jobs = [dict(j) for j in self._jobs.values()]
            scan = dict(self._scan)
        jobs.sort(key=lambda j: (
            {"run": 0, "wait": 1, "fail": 2, "done": 3}.get(j.get("state"), 4),
            j.get("seq") or 0,
        ))
        free = 0
        try:
            self.tmpdir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.tmpdir).free
        except Exception:
            pass
        stat = {"wait": 0, "run": 0, "done": 0, "fail": 0}
        for j in jobs:
            stat[j.get("state")] = stat.get(j.get("state"), 0) + 1
        return {
            "ffmpeg": ffmpeg_path(),
            "disk_free": free,
            "scan": scan,
            "jobs": jobs,
            "stat": stat,
            "running": bool(self._worker and self._worker.is_alive()),
            "paused": self._paused,
            "cfg": {
                "format": cfg.get("convert_format") or "m4a",
                "bitrate": int(cfg.get("convert_bitrate") or 96),
                "keep_origin": cfg.get("convert_keep_origin", True),
                "video": bool(cfg.get("convert_video", False)),
            },
        }

    # ---------------- 扫描 ----------------

    def scan_now(self, root_name: str):
        with self._lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return {"ok": False, "msg": "正在扫描，稍等"}
            self._scan = {"state": "run", "root": root_name, "books": [],
                          "msg": "正在读取书架…", "done": 0, "total": 0,
                          "at": time.time(), "err": ""}
            self._scan_thread = threading.Thread(
                target=self._scan_worker, args=(root_name,), daemon=True)
            self._scan_thread.start()
        return {"ok": True}

    def _scan_worker(self, root_name: str):
        try:
            client = self._client()
            root_id = client.find_folder(root_name, "-11")
            if not root_id:
                self._set_scan(state="done", msg="云盘里没有这个目录")
                return
            folders, loose = audiobook.list_all(client, root_id)
            self._set_scan(total=len(folders), msg="共 %d 本书，开始逐本检查" % len(folders))

            books = []
            done_n = 0
            for i, f in enumerate(folders, 1):
                name, author = audiobook.split_name(f["name"], "auto")
                try:
                    sub_folders, files = audiobook.list_all(client, f["id"])
                    for x in files:
                        x["_parent"] = f["id"]
                    for v in sub_folders:
                        try:
                            _, extra = audiobook.list_all(client, v["id"])
                            for x in extra:
                                x["_parent"] = v["id"]      # 分卷目录里的文件要传回原卷
                            files.extend(extra)
                        except TianyiError:
                            pass
                except TianyiError as e:
                    self._set_scan(done=i, msg="「%s」读取失败：%s" % (name, _fmt_err(e)))
                    continue
                books.append(self._one_book(f["id"], name, author, files))
                self._set_scan(done=i, msg="已检查 %d/%d 本" % (i, len(folders)))

            if loose:
                for x in loose:
                    x["_parent"] = root_id
                books.append(self._one_book(root_id, "未分类", "", loose))
            books = [b for b in books if b["pending"] or b["done"]]
            books.sort(key=lambda b: -len(b["pending"]))
            pending = sum(len(b["pending"]) for b in books)
            self._set_scan(state="done", books=books, done=len(folders),
                           msg="扫描完成：%d 个文件待转码" % pending)
        except Exception as e:
            self._set_scan(state="done", err=_fmt_err(e), msg="扫描失败：%s" % _fmt_err(e))

    def _one_book(self, fid: str, name: str, author: str, files: list) -> dict:
        playable_stems = set()
        for f in files:
            stem, ext = os.path.splitext(f["name"])
            if ext.lower() in PLAYABLE_EXT:
                playable_stems.add(stem.lower())

        pending, cleanup, done = [], [], 0
        with self._lock:
            jobs = dict(self._jobs)
        for f in files:
            ext = os.path.splitext(f["name"])[1].lower()
            kind = ""
            if ext in BAD_AUDIO_EXT:
                kind = "audio"
            elif ext in VIDEO_EXT:
                kind = "video"
            if not kind:
                continue
            stem = os.path.splitext(f["name"])[0].lower()
            job = jobs.get(f["id"]) or {}
            if job.get("state") == "done" or stem in playable_stems:
                done += 1
                # 同目录已经有能播的版本，原文件还在 → 列出来让用户自己决定删不删
                if stem in playable_stems and not job.get("origin_deleted"):
                    cleanup.append({
                        "id": f["id"], "name": f["name"], "ext": ext,
                        "size": int(f.get("size") or 0), "book": name,
                    })
                continue
            if job.get("state") in ("wait", "run"):
                continue                      # 已在队列里，不重复列
            pending.append({
                "id": f["id"], "name": f["name"], "ext": ext, "kind": kind,
                "size": int(f.get("size") or 0),
                "folder": f.get("_parent") or fid,
                "book": name,
                "fail": job.get("msg", "") if job.get("state") == "fail" else "",
            })
        return {"id": fid, "name": name, "author": author,
                "pending": pending, "done": done, "cleanup": cleanup}

    def _set_scan(self, **kw):
        with self._lock:
            self._scan.update(kw)

    # ---------------- 入队 ----------------

    def enqueue(self, items: list[dict]) -> int:
        """items: [{id, name, size, fid(所属目录)}]"""
        added = 0
        with self._lock:
            for it in items:
                fid = str(it.get("id") or "")
                if not fid:
                    continue
                cur = self._jobs.get(fid)
                if cur and cur.get("state") in ("wait", "run"):
                    continue
                self._seq += 1
                self._jobs[fid] = {
                    "id": fid,
                    "name": it.get("name") or fid,
                    "folder": str(it.get("folder") or ""),
                    "book": it.get("book") or "",
                    "size": int(it.get("size") or 0),
                    "state": "wait",
                    "stage": "",
                    "pct": 0.0,
                    "msg": "",
                    "out": "",
                    "seq": self._seq,
                    "at": int(time.time()),
                }
                added += 1
            if added:
                self._stop_flag = False
                self._paused = ""
                self._flush()
        if added:
            self._ensure_worker()
        return added

    def _ensure_worker(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def _next_job(self) -> dict | None:
        with self._lock:
            cand = [j for j in self._jobs.values() if j.get("state") == "wait"]
            if not cand:
                return None
            cand.sort(key=lambda j: j.get("seq") or 0)
            j = cand[0]
            j["state"] = "run"
            j["stage"] = "准备"
            j["pct"] = 0.0
            return j

    # ---------------- 干活 ----------------

    def _loop(self):
        while True:
            with self._lock:
                if self._stop_flag:
                    self._worker = None
                    self._stop_flag = False
                    return
            job = self._next_job()
            if job is None:
                with self._lock:
                    self._worker = None
                return
            try:
                self._run_one(job)
            except FlowOut as e:
                self._fail(job, str(e))
                with self._lock:
                    self._paused = str(e)
                    self._worker = None
                    # 剩下的保持 wait，等流量恢复了点「继续」再跑
                self._flush()
                return
            except Exception as e:                      # 兜底，绝不让线程死掉
                self._fail(job, _fmt_err(e))
            finally:
                self._flush()

    def _fail(self, job: dict, msg: str):
        with self._lock:
            job["state"] = "fail"
            job["stage"] = ""
            job["msg"] = msg
            job["pct"] = 0.0

    def _run_one(self, job: dict):
        from . import store
        cfg = store.load()
        out_fmt = (cfg.get("convert_format") or "m4a").lower()
        if out_fmt not in OUT_EXT:
            out_fmt = "m4a"
        kbps = int(cfg.get("convert_bitrate") or 96)
        out_ext = OUT_EXT[out_fmt]

        ff = ffmpeg_path()
        if not ff:
            raise TianyiError("服务器上没找到 ffmpeg，装一下再试")

        client = self._client()
        fid = job["id"]
        size = int(job.get("size") or 0)
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        stem = os.path.splitext(job["name"])[0]
        tmp_in = self.tmpdir / (fid + os.path.splitext(job["name"])[1].lower())
        tmp_out = self.tmpdir / (fid + out_ext)

        # 磁盘够不够：一个源文件 + 一个输出文件 + 200MB 余量
        free = shutil.disk_usage(self.tmpdir).free
        if free < size * 2 + 200 * 1024 * 1024:
            raise TianyiError("磁盘剩余空间不够，先清理一下")

        try:
            # 1. 下载
            with self._lock:
                job["stage"] = "下载"
                job["pct"] = 0.0
            url = client.get_download_url(fid)
            got = 0
            with httpx.stream("GET", url, follow_redirects=True,
                              timeout=httpx.Timeout(30.0, read=120.0)) as r:
                r.raise_for_status()
                total = size or int(r.headers.get("content-length") or 0) or 1
                with open(tmp_in, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        got += len(chunk)
                        with self._lock:
                            job["pct"] = round(min(got / total, 1) * 0.55, 3)
            if not tmp_in.exists() or tmp_in.stat().st_size == 0:
                raise TianyiError("下载下来是空文件，可能直链已过期")

            # 2. 转码
            with self._lock:
                job["stage"] = "转码"
                job["pct"] = 0.55
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(tmp_in), "-map", "0:a:0?", "-vn"]
            if out_fmt == "mp3":
                cmd += ["-c:a", "libmp3lame", "-b:a", "%dk" % kbps]
            else:
                cmd += ["-c:a", "aac", "-b:a", "%dk" % kbps,
                        "-movflags", "+faststart"]
            cmd += [str(tmp_out)]
            p = subprocess.run(cmd, capture_output=True, timeout=3600)
            if p.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
                tail = (p.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
                raise TianyiError("ffmpeg 转码失败：%s" % (tail[-1] if tail else "未知原因"))

            # 3. 上传
            with self._lock:
                job["stage"] = "上传"
                job["pct"] = 0.7

            def _cb(x):
                with self._lock:
                    job["pct"] = round(0.7 + x * 0.3, 3)

            out_name = stem + out_ext
            target = job.get("folder") or ""
            if not target:
                raise TianyiError("不知道这集在哪个目录，重新扫描一次")
            try:
                new_id = client.upload(str(tmp_out), target,
                                       display_name=out_name, progress_cb=_cb)
            except TianyiError as e:
                if _is_flow_out(e):
                    raise FlowOut("天翼提示「上传流量已用尽」，队列已暂停；"
                                  "等额度恢复后点「继续转码」接着跑")
                raise

            with self._lock:
                job["state"] = "done"
                job["stage"] = ""
                job["pct"] = 1.0
                job["msg"] = ""
                job["out"] = out_name
                job["out_id"] = new_id
                job["at"] = int(time.time())
            if self._on_done:
                try:
                    self._on_done(job, cfg)
                except Exception:
                    pass
        finally:
            for p in (tmp_in, tmp_out):
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            self._flush()

    # ---------------- 队列操作 ----------------

    def resume(self) -> dict:
        """流量恢复后继续跑队列里剩下的"""
        with self._lock:
            self._paused = ""
            self._stop_flag = False
            n = len([j for j in self._jobs.values() if j.get("state") == "wait"])
        if n:
            self._ensure_worker()
        return {"ok": True, "wait": n}

    def stop(self):
        with self._lock:
            self._stop_flag = True
            self._paused = ""
            for j in self._jobs.values():
                if j.get("state") == "wait":
                    j["state"] = "fail"
                    j["msg"] = "已手动停止"
            self._flush()
        return {"ok": True}

    def retry(self, ids: list[str] | None) -> int:
        n = 0
        with self._lock:
            for j in self._jobs.values():
                if j.get("state") != "fail":
                    continue
                if ids and j["id"] not in ids:
                    continue
                j["state"] = "wait"
                j["msg"] = ""
                j["pct"] = 0.0
                n += 1
            if n:
                self._stop_flag = False
                self._paused = ""
                self._flush()
        if n:
            self._ensure_worker()
        return n

    def remove(self, ids: list[str]) -> int:
        n = 0
        with self._lock:
            for fid in list(ids or []):
                j = self._jobs.get(fid)
                if j and j.get("state") != "run":
                    self._jobs.pop(fid, None)
                    n += 1
            if n:
                self._clear_pause_if_idle()
                self._flush()
        return n

    def clear_finished(self) -> int:
        with self._lock:
            ids = [k for k, j in self._jobs.items() if j.get("state") == "done"]
            for k in ids:
                self._jobs.pop(k, None)
            if ids:
                self._clear_pause_if_idle()
                self._flush()
        return len(ids)

    def _clear_pause_if_idle(self):
        """没有排队/在跑的任务了，暂停横幅就别挂着"""
        if not any(j.get("state") in ("wait", "run") for j in self._jobs.values()):
            self._paused = ""

    def delete_origin(self, fids: list[str]) -> dict:
        """删掉云盘里的原文件（只在转码成功后允许）"""
        client = self._client()
        allowed = set()
        with self._lock:
            for b in self._scan.get("books") or []:
                for c in b.get("cleanup") or []:
                    allowed.add(c["id"])
            for k, j in self._jobs.items():
                if j.get("state") == "done":
                    allowed.add(k)
        ok, bad = 0, []
        for fid in fids or []:
            with self._lock:
                j = dict(self._jobs.get(fid) or {})
            if fid not in allowed:
                bad.append({"id": fid, "why": "这个文件还不能删（没有可播的替代版本）"})
                continue
            try:
                client.delete_files([fid])
                ok += 1
                with self._lock:
                    if fid in self._jobs:
                        self._jobs[fid]["origin_deleted"] = True
                        self._flush()
            except TianyiError as e:
                bad.append({"id": fid, "why": _fmt_err(e)})
        return {"ok": ok, "bad": bad}
