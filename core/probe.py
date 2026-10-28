"""音源健康探测：后台定时体检每个源，自动选出当前环境可用的

部署到任何机器（海外机 / 国内机 / 家庭电脑）后无需配置，
启动几分钟内自动知道哪些源能搜、哪些源能下，并按速度排序使用。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .sources import all_sources

PROBE_INTERVAL = 30 * 60  # 半小时复检一次
PROBE_KEYWORD = "晴天"

_lock = threading.Lock()
_state: dict = {"sources": [], "updated": 0.0, "probing": False}
_started = False
_state_file: Path | None = None


def _load_state():
    """重启后沿用上次体检结果，避免开机一段时间内全是“检测中”"""
    global _state
    if not _state_file or not _state_file.exists():
        return
    try:
        d = json.loads(_state_file.read_text("utf-8"))
        if isinstance(d, dict) and isinstance(d.get("sources"), list) and d["sources"]:
            with _lock:
                _state["sources"] = d["sources"]
                _state["updated"] = float(d.get("updated") or 0)
    except Exception:
        pass


def _save_state():
    if not _state_file:
        return
    try:
        with _lock:
            data = {"updated": _state["updated"], "sources": [dict(s) for s in _state["sources"]]}
        _state_file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception:
        pass


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
    """体检单个源：失败再试一次，避免一次网络抖动就判成不可用"""
    t0 = time.time()
    ok = False
    for attempt in range(2):
        try:
            if src.probe():
                ok = True
                break
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1.5)
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
                # 已重测的用新结果，还没轮到的沿用上次结果，避免列表中途缩水
                merged = {s["name"]: dict(s) for s in _state["sources"]}
                for x in results:
                    merged[x["name"]] = x
                _state["sources"] = sorted(
                    merged.values(), key=lambda x: (not x["ok"], x["latency"])
                )
        with _lock:
            _state["updated"] = time.time()
            _state["probing"] = False
        _save_state()

    if background:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()


def start(state_file=None):
    """应用启动时调用：先载入上次结果，再后台复检"""
    global _started, _state_file
    if _started:
        return
    _started = True
    if state_file:
        _state_file = Path(state_file)
    _load_state()

    def _loop():
        probe_all(background=False)  # 首测同步跑，尽快给出可用源
        while True:
            time.sleep(PROBE_INTERVAL)
            try:
                probe_all(background=False)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()
