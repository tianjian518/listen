"""音源注册表：新部署时由探测系统自动选出可用音源"""
from __future__ import annotations

from .base import MusicSource
from .gdstudio import GDStudioSource
from .invidious import InvidiousSource
from .youtube import YouTubeSource
from .netease import NeteaseSource
from .bilibili import BilibiliSource


def all_sources() -> list[MusicSource]:
    return [GDStudioSource(), InvidiousSource(), YouTubeSource(), NeteaseSource(), BilibiliSource()]


def get_source(name: str) -> MusicSource | None:
    for s in all_sources():
        if s.name == name:
            return s
    return None
