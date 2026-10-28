"""转存任务队列：下载 -> 上传云盘 -> 清理"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media, store
from .tianyi import TianyiClient, TianyiError

MAX_KEEP = 60


class TaskManager:
    def __init__(self, download_dir: Path):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transfer")

    # ---------- 查询 ----------

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(self._tasks[i]) for i in self._order if i in self._tasks]

    def get(self, tid: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t) if t else None

    def _update(self, tid: str, **kw):
        with self._lock:
            if tid in self._tasks:
                self._tasks[tid].update(kw)
                self._tasks[tid]["updated"] = time.time()

    # ---------- 提交 ----------

    def submit(self, video_id: str, title: str, source: str = "invidious",
               host: str = "") -> str:
        tid = uuid.uuid4().hex[:12]
        task = {
            "id": tid,
            "video_id": video_id,
            "title": title,
            "source": source or "invidious",
            "host": host or "",
            "state": "queued",
            "stage": "排队中",
            "progress": 0.0,
            "message": "",
            "size": 0,
            "created": time.time(),
            "updated": time.time(),
        }
        with self._lock:
            self._tasks[tid] = task
            self._order.append(tid)
            while len(self._order) > MAX_KEEP:
                old = self._order.pop(0)
                self._tasks.pop(old, None)

        self._pool.submit(self._run, tid)
        return tid

    def retry(self, tid: str) -> str | None:
        t = self.get(tid)
        if not t or t["state"] not in ("failed", "done"):
            return None
        return self.submit(t["video_id"], t["title"])

    # ---------- 执行 ----------

    def _run(self, tid: str):
        self._update(tid, state="running")
        local: Path | None = None
        try:
            t = self.get(tid)
            if not t:
                return
            cfg = store.load()

            # 1) 下载
            self._update(tid, stage="下载中", progress=0.0, message="正在从音源下载…")
            local = media.download_audio(
                t["video_id"],
                t["title"],
                self.download_dir,
                quality=str(cfg.get("quality", "0")),
                progress_cb=lambda p: self._update(tid, progress=round(p, 3)),
                source=t.get("source", "invidious"),
                host=t.get("host", ""),
            )
            size = local.stat().st_size if local.exists() else 0
            self._update(tid, stage="下载完成", size=size, progress=1.0)

            # 2) 连接云盘
            if not cfg.get("cookie"):
                raise TianyiError("还没登录天翼云盘，先在右上角扫码登录")

            self._update(tid, stage="连接云盘", message="正在连接天翼云盘…")
            client = TianyiClient(cfg["cookie"])
            client.connect()

            folder_id = str(cfg.get("folder_id") or "-11")
            if folder_id == "-11" and cfg.get("auto_create", True):
                folder_name = (cfg.get("folder_name") or "我的音乐").strip("/")
                try:
                    folder_id = client.ensure_folder(folder_name, "-11")
                except Exception:
                    # 建目录失败就退回根目录，别让整单任务挂掉
                    folder_id = "-11"

            # 3) 上传
            self._update(tid, stage="上传中", progress=0.0, message="正在传到云盘…")
            client.upload(
                str(local),
                folder_id,
                display_name=local.name,
                progress_cb=lambda p: self._update(tid, progress=round(p, 3)),
            )
            client.close()

            # 4) 清理
            media.cleanup(local)
            self._update(
                tid,
                state="done",
                stage="已完成",
                progress=1.0,
                message="已保存到云盘",
            )
        except TianyiError as e:
            self._update(tid, state="failed", stage="失败", message=str(e))
            if local:
                media.cleanup(local)
        except Exception as e:
            raw = str(e)
            if "Sign in to confirm" in raw or "not a bot" in raw:
                msg = "音源要求身份验证：请点顶部「音源」上传 YouTube 的 cookies.txt（一次性设置）"
            else:
                msg = f"{type(e).__name__}: {raw[:180]}"
            self._update(tid, state="failed", stage="失败", message=msg)
            traceback.print_exc()
            if local:
                media.cleanup(local)
