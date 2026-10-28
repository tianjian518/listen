"""GD 音乐台聚合源：聚合网易/QQ等多个平台，最高 320k

用 curl_cffi 模拟真实浏览器指纹过 Cloudflare。
部分网络环境可能仍被拦截，由探测系统自动判断是否启用。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from .base import MusicSource

API = "https://music-api.gdstudio.xyz/api.php"
_DL_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
               "Referer": "https://music.163.com/"}


class GDStudioSource(MusicSource):
    name = "gdstudio"
    label = "聚合音源"
    note = "聚合网易/QQ等平台，最高 320k 高音质。被 Cloudflare 拦截的网络会自动停用"
    habitat = "any"

    def _get(self, params: dict):
        from curl_cffi import requests as creq

        r = creq.get(API, params=params, impersonate="chrome", timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"GD 接口 HTTP {r.status_code}")
        return r.json()

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        d = self._get({
            "types": "search", "source": "netease", "name": keyword,
            "count": str(max(1, min(limit, 30))), "pages": "1", "br": "320",
        })
        if not isinstance(d, list):
            return []
        out = []
        for e in d:
            if not e.get("id"):
                continue
            artist = e.get("artist")
            artists = ", ".join(artist) if isinstance(artist, list) else str(artist or "")
            out.append({
                "id": str(e["id"]),
                "title": e.get("name") or "未知",
                "uploader": artists,
                "duration": 0,
                "duration_text": "--:--",
                "thumb": "",
                "source": "gdstudio",
                "host": "",
                "gd_source": e.get("source", "netease"),
            })
        return out

    def download(self, item: dict, out_dir: Path, progress_cb=None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = MusicSource.clean_name(item["title"])
        target = out_dir / f"{safe}.mp3"
        if target.exists() and target.stat().st_size > 10240:
            if progress_cb:
                progress_cb(1.0)
            return target

        gd_source = item.get("gd_source", "netease")
        url = ""
        for br in (320, 192, 128):
            try:
                d = self._get({"types": "url", "source": gd_source,
                               "id": str(item["id"]), "br": str(br)})
                url = (d or {}).get("url") if isinstance(d, dict) else ""
                if url:
                    break
            except Exception:
                continue
        if not url:
            raise RuntimeError("该歌曲需要会员或暂时无音源，换一首试试")

        ext = ".m4a" if ".m4a" in url else (".flac" if ".flac" in url else ".mp3")
        tmp = out_dir / f".gd_{item['id']}{ext}"
        MusicSource.stream_download(url, tmp, headers=_DL_HEADERS, progress_cb=progress_cb)
        try:
            if target.exists():
                target.unlink()
            if ext == ".mp3":
                shutil.move(str(tmp), str(target))
            else:
                MusicSource.ffmpeg_to_mp3(tmp, target, kbps=192)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        if progress_cb:
            progress_cb(1.0)
        return target

    def probe(self) -> bool:
        """搜索 + 真实下载 64KB 双验证"""
        try:
            items = self.search("晴天", 3)
            if not items:
                return False
            url = ""
            for it in items:
                try:
                    d = self._get({"types": "url", "source": it.get("gd_source", "netease"),
                                   "id": str(it["id"]), "br": "128"})
                    url = (d or {}).get("url") if isinstance(d, dict) else ""
                    if url:
                        break
                except Exception:
                    continue
            if not url:
                return False
            got = 0
            with httpx.stream("GET", url, headers=_DL_HEADERS, timeout=30,
                              follow_redirects=True) as s:
                if s.status_code != 200:
                    return False
                for chunk in s.iter_bytes(32768):
                    got += len(chunk)
                    if got >= 65536:
                        break
            return got >= 65536
        except Exception:
            return False
