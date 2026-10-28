"""B 站音乐区源：国内网络可用，中文 MV/翻唱/无损投稿多（需要 yt-dlp）"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .base import MusicSource, ydl_opts

_ydl_lock = __import__("threading").Lock()


def _ensure_cookie_file() -> Path:
    """B 站 412 风控需要至少一个 buvid3，伪造即可"""
    p = Path("/tmp/.bili_cookie.txt")
    if p.exists():
        return p
    buvid3 = f"{uuid.uuid4()}infoc"
    buvid4 = str(uuid.uuid4())
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        f".bilibili.com\tTRUE\t/\tTRUE\t0\tbuvid3\t{buvid3}\n"
        f".bilibili.com\tTRUE\t/\tTRUE\t0\tb_nut\t1730000000\n"
        f".bilibili.com\tTRUE\t/\tTRUE\t0\tbuvid4\t{buvid4}\n"
    )
    return p


class BilibiliSource(MusicSource):
    name = "bilibili"
    label = "B 站音乐区"
    note = "国内网络可用；MV/翻唱/演唱会资源多，128k~192k"
    habitat = "china"

    def _opts(self) -> dict:
        opts = ydl_opts()
        opts["cookiefile"] = str(_ensure_cookie_file())
        return opts

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        from yt_dlp import YoutubeDL

        opts = self._opts()
        opts.update({"extract_flat": True, "skip_download": True})
        n = max(1, min(limit, 30))
        with _ydl_lock:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"bilisearch{n}:{keyword}", download=False)
        out = []
        for e in (info or {}).get("entries", []) or []:
            if not e or not e.get("id"):
                continue
            dur = e.get("duration") or 0
            out.append({
                "id": e["id"],
                "title": e.get("title") or "未知标题",
                "uploader": e.get("uploader") or "",
                "duration": int(dur) if dur else 0,
                "duration_text": MusicSource.fmt_duration(dur),
                "thumb": e.get("thumbnails", [{}])[0].get("url", "")
                if e.get("thumbnails") else "",
                "source": "bilibili",
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
        tmp = out_dir / f".bili_{vid}.%(ext)s"

        def hook(d):
            if d.get("status") == "downloading" and progress_cb:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                if total:
                    progress_cb(min(got / total * 0.95, 0.95))
            elif d.get("status") == "finished" and progress_cb:
                progress_cb(0.97)

        opts = self._opts()
        opts.update({
            "format": "ba/best",
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
                ydl.download([f"https://www.bilibili.com/video/{vid}"])

        produced = None
        for f in out_dir.glob(f".bili_{vid}.*"):
            if f.suffix.lower() == ".mp3":
                produced = f
                break
        if produced is None:
            raise RuntimeError("B 站下载完成但没找到 mp3")
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(produced), str(target))
        except Exception:
            target = produced
        for leftover in out_dir.glob(f".bili_{vid}.*"):
            try:
                leftover.unlink()
            except Exception:
                pass
        if progress_cb:
            progress_cb(1.0)
        return target

    def probe(self) -> bool:
        """必须验证到「能拿到播放流」才算可用：B站搜索时通时断，搜索通过不代表能下载"""
        try:
            items = self.search("晴天", 3)
            if not items:
                return False
            from yt_dlp import YoutubeDL

            opts = self._opts()
            opts.update({"skip_download": True, "quiet": True})
            with _ydl_lock:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(
                        f"https://www.bilibili.com/video/{items[0]['id']}",
                        download=False,
                    )
            return bool(info and info.get("formats"))
        except Exception:
            return False
