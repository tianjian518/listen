"""音源调度层：自动选路 + 兼容旧接口"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .probe import healthy_sources
from .sources import get_source
from .sources.base import (
    COOKIE_FILE,
    clean_name,
    clear_yt_cookie,
    fmt_duration,
    has_yt_cookie,
    save_yt_cookie,
)


def search(keyword: str, limit: int = 18) -> list[dict]:
    """按探测健康度依次尝试各音源，返回第一个有结果的列表"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    for src in healthy_sources():
        try:
            items = src.search(keyword, limit)
            if items:
                return items
        except Exception:
            continue
    return []


def download_audio(video_id: str, title: str, out_dir: Path,
                   quality: str = "0", progress_cb=None,
                   source: str = "invidious", host: str = "") -> Path:
    src = get_source(source)
    if src is None:
        raise RuntimeError(f"未知音源：{source}")
    item = {"id": video_id, "title": title, "source": source, "host": host}
    return src.download(item, out_dir, progress_cb)


# ---- 兼容旧调用（app.py 使用） ----

def has_cookie() -> bool:
    return has_yt_cookie()


def cleanup(path: Path | None):
    """上传成功后删掉本地临时文件"""
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def disk_free_gb() -> float:
    try:
        return shutil.disk_usage(
            os.environ.get("MUSIC_PANEL_DOWNLOADS", "/tmp")
        ).free / (1024 ** 3)
    except Exception:
        return 0.0
