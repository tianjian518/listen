"""音源健康探测：后台定时体检每个源，自动选出当前环境可用的

部署到任何机器（海外机 / 国内机 / 家庭电脑）后无需配置，
启动几分钟内自动知道哪些源能搜、哪些源能下，并按速度排序使用。
"""
from __future__ import annotations

import threading
import time

from .sources import all_sources

PROBE_INTERVAL = 30 * 60  # 半小时复检一次
PROBE_KEYWORD = "晴天"

_lock = threading.Lock()
_state: dict = {"sources": [], "updated": 0.0, "probing": False}
_started = False


def snapshot() -> dict:
    with _lock:
        return {
            "sources": [dict(s) for s in _state["sources"]],
            "updated": _state["updated"],
            "probing": _state["probing"],
        }


def healthy_sources() -> list:
    """按 (可用, 延迟) 排序的健康源实例列表"""
    with _lock:
        ranked = [s["name"] for s in _state["sources"] if s.get("ok")]
    if not ranked:
        # 还没探测完：按默认顺序全部返回，让调用方自己试
        return all_sources()
    by_name = {s.name: s for s in all_sources()}
    ordered = [by_name[n] for n in ranked if n in by_name]
    ordered += [s for n, s in by_name.items() if n not in ranked]
    return ordered


def _probe_one(src) -> dict:
    t0 = time.time()
    ok = False
    try:
        ok = bool(src.probe())
    except Exception:
        ok = False
    return {
        "name": src.name,
        "label": src.label,
        "note": src.note,
        "habitat": src.habitat,
        "ok": ok,
        "latency": round(time.time() - t0, 2),
    }


def probe_all(background: bool = True):
    """全量体检。background=True 时在独立线程跑，不阻塞请求"""
    def _run():
        global _state
        with _lock:
            _state["probing"] = True
        results = []
        for src in all_sources():
            results.append(_probe_one(src))
            with _lock:
                _state["sources"] = sorted(
                    results, key=lambda x: (not x["ok"], x["latency"])
                )
        with _lock:
            _state["updated"] = time.time()
            _state["probing"] = False

    if background:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()


def start():
    """应用启动时调用：立即首测 + 定时复检"""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        probe_all(background=False)  # 首测同步跑，尽快给出可用源
        while True:
            time.sleep(PROBE_INTERVAL)
            try:
                probe_all(background=False)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()
