"""轻量配置存储（config.json）"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("MUSIC_PANEL_CONFIG", BASE_DIR / "config.json"))
DOWNLOAD_DIR = Path(os.environ.get("MUSIC_PANEL_DOWNLOADS", BASE_DIR / "downloads"))

_lock = threading.Lock()
_default = {
    "cookie": "",
    "account": "",
    "folder_id": "-11",
    "folder_name": "我的音乐",
    "auto_create": True,
    "quality": "0",
    "custom_hosts": [],
    "book_folder": "有声书",
    "book_name_order": "auto",
    "convert_format": "m4a",
    "convert_bitrate": 96,
    "convert_keep_origin": True,
    "convert_video": False,
}


def load() -> dict:
    with _lock:
        data = dict(_default)
        if CONFIG_PATH.exists():
            try:
                data.update(json.loads(CONFIG_PATH.read_text("utf-8")))
            except Exception:
                pass
        return data


def save(**kw) -> dict:
    with _lock:
        data = dict(_default)
        if CONFIG_PATH.exists():
            try:
                data.update(json.loads(CONFIG_PATH.read_text("utf-8")))
            except Exception:
                pass
        data.update({k: v for k, v in kw.items() if v is not None})
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        try:
            CONFIG_PATH.chmod(0o600)
        except Exception:
            pass
        return data


def save_cookie(cookie: str):
    save(cookie=cookie)


def get_cookie() -> str:
    return load().get("cookie", "")


def clear_cookie():
    save(cookie="", account="")


def get_custom_hosts() -> list:
    return [h for h in (load().get("custom_hosts") or []) if h]


def save_custom_hosts(hosts: list) -> list:
    clean = []
    for h in hosts:
        h = str(h).strip()
        if not h:
            continue
        h = re.sub(r"^https?://", "", h)
        h = h.split("/")[0].strip().lower()
        if h and h not in clean and "." in h or ":" in h:
            clean.append(h)
    save(custom_hosts=clean)
    return clean
