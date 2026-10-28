"""网易云音乐源：国内网络首选，免登录（仅免费音质，VIP 歌自动跳过）"""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from .base import MusicSource, UA

API = "https://music.163.com"


class NeteaseSource(MusicSource):
    name = "netease"
    label = "网易云音乐"
    note = "国内网络首选，中文曲库最全；VIP 歌曲不可下载会自动跳过"
    habitat = "china"

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        r = httpx.post(
            f"{API}/api/search/get",
            data={"s": keyword, "type": 1, "limit": str(limit), "offset": "0"},
            headers={**UA, "Referer": "https://music.163.com/"},
            timeout=15,
        )
        songs = (r.json() or {}).get("songs") or []
        out = []
        for s in songs:
            artists = "/".join(a.get("name", "") for a in (s.get("artists") or []))
            dur_ms = int(s.get("duration") or 0)
            out.append({
                "id": str(s["id"]),
                "title": s.get("name") or "未知",
                "uploader": artists,
                "duration": dur_ms // 1000,
                "duration_text": MusicSource.fmt_duration(dur_ms // 1000),
                "thumb": (s.get("album") or {}).get("picUrl", ""),
                "source": "netease",
                "host": "",
            })
        return out

    def _get_play_url(self, song_id: str) -> tuple[str, str]:
        r = httpx.post(
            f"{API}/api/song/enhance/player/url",
            data={"ids": f"[{song_id}]", "br": "320000"},
            headers={**UA, "Referer": "https://music.163.com/"},
            timeout=15,
        )
        d = (r.json().get("data") or [{}])[0]
        url = d.get("url") or ""
        if not url:
            raise RuntimeError("该歌曲需要 VIP 或无版权，换一首试试")
        ext = "m4a" if ".m4a" in url else "mp3"
        return url, ext

    def download(self, item: dict, out_dir: Path, progress_cb=None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = MusicSource.clean_name(item["title"])
        target = out_dir / f"{safe}.mp3"
        if target.exists() and target.stat().st_size > 10240:
            if progress_cb:
                progress_cb(1.0)
            return target

        url, ext = self._get_play_url(item["id"])
        tmp = out_dir / f".netease_{item['id']}.{ext}"
        MusicSource.stream_download(url, tmp,
                                    headers={"Referer": "https://music.163.com/"},
                                    progress_cb=progress_cb)
        try:
            if target.exists():
                target.unlink()
            if ext == "mp3":
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
        """搜索有结果且第一首能拿到真实下载地址才算可用（海外 IP 拿不到）"""
        try:
            items = self.search("晴天", 3)
            if not items:
                return False
            url, _ = self._get_play_url(items[0]["id"])
            return bool(url)
        except Exception:
            return False
