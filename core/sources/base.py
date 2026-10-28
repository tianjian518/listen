"""音源基类与注册表：每个音源实现 search() 和 download()，探测系统自动选路"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
COOKIE_FILE = Path(os.environ.get("YT_COOKIE_FILE", BASE_DIR / "yt_cookies.txt"))

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"}

BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
SUFFIX_NOISE = re.compile(
    r"\s*[\(\[]\s*(official\s*(music\s*)?video|official\s*audio|lyric\s*video|"
    r"lyrics?|audio|mv|hd|4k|高音质|无损|完整版|官方)\s*[\)\]]",
    re.IGNORECASE,
)


def clean_name(raw: str, limit: int = 120) -> str:
    name = SUFFIX_NOISE.sub("", raw or "")
    name = BAD_CHARS.sub(" ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .-")
    return (name or "未知歌曲")[:limit]


def fmt_duration(sec) -> str:
    try:
        s = int(sec or 0)
    except Exception:
        return "--:--"
    if s <= 0:
        return "--:--"
    h, rem = divmod(s, 3600)
    m, s2 = divmod(rem, 60)
    return f"{h}:{m:02d}:{s2:02d}" if h else f"{m}:{s2:02d}"


class MusicSource:
    """所有音源的统一接口"""

    name: str = "base"
    label: str = "未命名"
    note: str = ""
    # 典型适用环境（仅用于展示）：oversea / china / any
    habitat: str = "any"

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        raise NotImplementedError

    def download(self, item: dict, out_dir: Path, progress_cb=None) -> Path:
        raise NotImplementedError

    def probe(self) -> bool:
        """健康探针，默认搜一次；子类可覆盖更精确的逻辑"""
        try:
            return len(self.search("晴天", 3)) > 0
        except Exception:
            return False

    # ---------- 通用工具 ----------

    @staticmethod
    def ffmpeg_to_mp3(src: Path, dst: Path, kbps: int = 192) -> Path:
        p = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn",
             "-acodec", "libmp3lame", "-b:a", f"{kbps}k", str(dst)],
            capture_output=True, text=True, timeout=300,
        )
        if p.returncode != 0 or not dst.exists():
            raise RuntimeError("转码失败：" + (p.stderr or "")[-160:])
        return dst

    @staticmethod
    def stream_download(url: str, dest: Path, headers: dict | None = None,
                        progress_cb=None, timeout: int = 180) -> int:
        got = 0
        with httpx.stream("GET", url, headers=headers or UA, timeout=timeout,
                          follow_redirects=True) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or 0)
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(131072):
                    f.write(chunk)
                    got += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(got / total * 0.9, 0.9))
        if got < 100000:
            raise RuntimeError(f"文件过小（{got} 字节），可能被风控")
        return got


# ---------------- yt-dlp / YouTube cookie 工具 ----------------

def has_yt_cookie() -> bool:
    try:
        return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 60
    except Exception:
        return False


def save_yt_cookie(content: bytes) -> bool:
    text = content.decode("utf-8", errors="ignore")
    if "youtube.com" not in text and "googlevideo" not in text:
        return False
    COOKIE_FILE.write_bytes(content)
    try:
        COOKIE_FILE.chmod(0o600)
    except Exception:
        pass
    return True


def clear_yt_cookie():
    try:
        COOKIE_FILE.unlink()
    except Exception:
        pass


def ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
        "geo_bypass": True,
        "cachedir": False,
    }
    if has_yt_cookie():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


# 把模块级工具函数挂为静态方法，供各音源以 MusicSource.xxx 调用
MusicSource.clean_name = staticmethod(clean_name)
MusicSource.fmt_duration = staticmethod(fmt_duration)
