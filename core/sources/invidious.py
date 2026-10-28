"""Invidious 公益镜像源：无需任何配置，海外网络首选"""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from .base import MusicSource, UA, fmt_duration

INV_HOSTS = [
    "yt.chocolatemoo53.com",
    "invidious.tiekoetter.com",
    "inv.nadeko.net",
    "invidious.nerdvpn.de",
    "iv.melmac.space",
]


def _custom_hosts() -> list[str]:
    """用户在设置页添加的自定义实例（探活可用时优先使用）"""
    try:
        from .. import store
        return [h for h in (store.load().get("custom_hosts") or []) if h]
    except Exception:
        return []


def all_known_hosts() -> list[str]:
    """内置 + 自定义，自定义排前面"""
    custom = _custom_hosts()
    return custom + [h for h in INV_HOSTS if h not in custom]


class InvidiousSource(MusicSource):
    name = "invidious"
    label = "公益镜像"
    note = "志愿者架设的公共镜像，无需设置，海外网络首选。速度较慢（2~4分钟/首）"
    habitat = "oversea"

    def __init__(self):
        self._ok_host = ""

    def _hosts(self):
        # 用户自定义实例优先，其次探测成功的，最后默认列表
        preferred = self._ok_host
        hosts = all_known_hosts()
        if preferred:
            return [preferred] + [h for h in hosts if h != preferred]
        return hosts

    def search(self, keyword: str, limit: int = 18) -> list[dict]:
        for host in self._hosts():
            try:
                r = httpx.get(
                    f"https://{host}/api/v1/search",
                    params={"q": keyword, "type": "video"},
                    headers=UA, timeout=20,
                )
                if r.status_code != 200 or not r.text.lstrip().startswith("["):
                    continue
                out = []
                for e in r.json()[:limit]:
                    if not e.get("videoId"):
                        continue
                    dur = int(e.get("lengthSeconds") or 0)
                    out.append({
                        "id": e["videoId"],
                        "title": e.get("title") or "未知标题",
                        "uploader": e.get("author") or "",
                        "duration": dur,
                        "duration_text": fmt_duration(dur),
                        "thumb": f"https://{host}/vi/{e['videoId']}/mqdefault.jpg",
                        "source": "invidious",
                        "host": host,
                    })
                if out:
                    return out
            except Exception:
                continue
        return []

    def download(self, item: dict, out_dir: Path, progress_cb=None) -> Path:
        host = item.get("host") or self._hosts()[0]
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{MusicSource.clean_name(item['title'])}.mp3"
        tmp = out_dir / f".{item['id']}.m4a"
        errors = []
        for itag in (140, 251):
            url = f"https://{host}/latest_version?id={item['id']}&itag={itag}&local=true"
            try:
                MusicSource.stream_download(url, tmp, progress_cb=progress_cb)
                break
            except Exception as e:
                errors.append(f"itag{itag}: {str(e)[:50]}")
        else:
            raise RuntimeError(f"公益镜像下载失败（{'; '.join(errors)[:120]}）")
        try:
            if target.exists():
                target.unlink()
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
        """搜索通过 + 代理直链真能出音频，才算可用"""
        for host in all_known_hosts():
            try:
                r = httpx.get(
                    f"https://{host}/api/v1/search",
                    params={"q": "晴天", "type": "video"},
                    headers=UA, timeout=15,
                )
                if r.status_code != 200 or not r.text.lstrip().startswith("[") or not r.json():
                    continue
                vid = r.json()[0].get("videoId")
                if not vid:
                    continue
                # 验证下载链路：拉代理直链的前几 KB
                url = f"https://{host}/latest_version?id={vid}&itag=140&local=true"
                got = 0
                with httpx.stream("GET", url, headers=UA, timeout=25,
                                  follow_redirects=True) as s:
                    if s.status_code != 200:
                        continue
                    for chunk in s.iter_bytes(32768):
                        got += len(chunk)
                        if got >= 65536:
                            break
                if got >= 65536:
                    self._ok_host = host
                    return True
            except Exception:
                continue
        return False
