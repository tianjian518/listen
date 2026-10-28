"""YouTube 直连源（yt-dlp）：海外网络可用，上传 cookies.txt 后最佳"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import MusicSource, has_yt_cookie, ydl_opts

_ydl_lock = __import__("threading").Lock()


class YouTubeSource(MusicSource):
    name = "youtube"
    label = "YouTube 直连"
    note = "需海外网络；上传 cookies.txt 后最稳最快（约30秒/首）"
    habitat = "oversea"

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        from yt_dlp import YoutubeDL

        opts = ydl_opts()
        opts.update({"extract_flat": True, "skip_download": True})
        n = max(1, min(limit, 30))
        with _ydl_lock:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{n}:{keyword}", download=False)
        out = []
        for e in (info or {}).get("entries", []) or []:
            if not e or not e.get("id"):
                continue
            dur = e.get("duration") or 0
            out.append({
                "id": e["id"],
                "title": e.get("title") or "未知标题",
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": int(dur) if dur else 0,
                "duration_text": MusicSource.fmt_duration(dur),
                "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                "source": "youtube",
                "host": "",
            })
        return out

    def download(self, item: dict, out_dir: Path, progress_cb=None) -> Path:
        from yt_dlp import YoutubeDL

        out_dir.mkdir(parents=True, exist_ok=True)
        safe = MusicSource.clean_name(item["title"])
        target = out_dir / f"{safe}.mp3"
        if target.exists() and target.stat().st_size > 10240:
            if progress_cb:
                progress_cb(1.0)
            return target

        vid = item["id"]
        tmp = out_dir / f".{vid}.%(ext)s"

        def hook(d):
            if d.get("status") == "downloading" and progress_cb:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                if total:
                    progress_cb(min(got / total * 0.95, 0.95))
            elif d.get("status") == "finished" and progress_cb:
                progress_cb(0.97)

        opts = ydl_opts()
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": str(tmp),
            "progress_hooks": [hook],
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        })
        with _ydl_lock:
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={vid}"])

        produced = None
        for f in out_dir.glob(f".{vid}.*"):
            if f.suffix.lower() == ".mp3":
                produced = f
                break
        if produced is None:
            raise RuntimeError("下载完成但没找到 mp3 文件")
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(produced), str(target))
        except Exception:
            target = produced
        for leftover in out_dir.glob(f".{vid}.*"):
            try:
                leftover.unlink()
            except Exception:
                pass
        if progress_cb:
            progress_cb(1.0)
        return target

    def probe(self) -> bool:
        """无 cookie 时搜索能通但下载会被风控，视为不可用，避免误选"""
        if not has_yt_cookie():
            return False
        try:
            return len(self.search("晴天", 3)) > 0
        except Exception:
            return False
