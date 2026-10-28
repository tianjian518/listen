"""转存任务队列：下载 -> 上传云盘 -> 清理"""
from __future__ import annotations

import json
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
    def __init__(self, download_dir: Path, state_file: Path | None = None):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file) if state_file else self.download_dir.parent / "tasks.json"
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transfer")
        self._dirty = False
        self._stop = threading.Event()
        self._load()
        threading.Thread(target=self._saver_loop, daemon=True).start()

    # ---------- 持久化 ----------

    def _load(self):
        """启动时恢复历史记录；上次没跑完的任务标为中断，可手动重试"""
        try:
            if not self.state_file.exists():
                return
            data = json.loads(self.state_file.read_text("utf-8"))
            if not isinstance(data, list):
                return
            for t in data:
                tid = t.get("id")
                if not tid:
                    continue
                if t.get("state") in ("running", "queued"):
                    t["state"] = "failed"
                    t["stage"] = "已中断"
                    t["message"] = "服务重启导致中断，可点重试"
                self._tasks[tid] = t
                self._order.append(tid)
            while len(self._order) > MAX_KEEP:
                old = self._order.pop(0)
                self._tasks.pop(old, None)
        except Exception:
            pass

    def _save(self):
        try:
            data = [self._tasks[i] for i in self._order if i in self._tasks]
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self.state_file)
        except Exception:
            pass

    def _saver_loop(self):
        """每 2 秒把变更落盘一次，避免频繁写文件"""
        while not self._stop.is_set():
            self._stop.wait(2.0)
            if self._dirty:
                self._dirty = False
                self._save()

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
                self._dirty = True

    # ---------- 提交 ----------

    def submit(self, video_id: str, title: str, source: str = "invidious",
               host: str = "", gid: str = "", cover: str = "") -> str:
        tid = uuid.uuid4().hex[:12]
        task = {
            "id": tid,
            "video_id": video_id,
            "title": title,
            "source": source or "invidious",
            "host": host or "",
            "gid": gid or "",
            "cover": cover or "",
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
            self._dirty = True

        self._pool.submit(self._run, tid)
        return tid

    def retry(self, tid: str) -> str | None:
        """原地重试：复用同一条记录重新排队，列表里不会多出一行重复的"""
        t = self.get(tid)
        if not t or t["state"] not in ("failed", "done"):
            return None
        with self._lock:
            if tid not in self._tasks:
                return None
            self._tasks[tid].update({
                "state": "queued",
                "stage": "排队中",
                "progress": 0.0,
                "message": "已重新提交",
                "size": 0,
                "created": time.time(),
                "updated": time.time(),
            })
            self._dirty = True
        self._pool.submit(self._run, tid)
        return tid

    def retry_all_failed(self) -> int:
        """一键重试：把所有失败任务重新排队，返回提交数量"""
        failed = [t for t in self.list() if t["state"] == "failed"]
        n = 0
        for t in failed:
            if self.retry(t["id"]):
                n += 1
        return n

    def remove(self, tid: str) -> bool:
        """删除单条记录；运行中/排队中的不允许删，避免删掉还在写文件的任务"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t["state"] in ("running", "queued"):
                return False
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)
            self._dirty = True
            return True

    def clear_finished(self) -> int:
        """批量清除已结束（完成 / 失败）的记录，运行中的保留"""
        removed = 0
        with self._lock:
            keep: list[str] = []
            for tid in self._order:
                t = self._tasks.get(tid)
                if not t:
                    continue
                if t["state"] in ("done", "failed"):
                    self._tasks.pop(tid, None)
                    removed += 1
                else:
                    keep.append(tid)
            self._order = keep
            if removed:
                self._dirty = True
        return removed

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
                gid=t.get("gid", ""),
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
