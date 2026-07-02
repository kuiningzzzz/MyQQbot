"""Direct news/trending-source adapters.

The endpoint choices and field mappings in this module are adapted from
NewsNow (https://github.com/ourongxing/newsnow), Copyright (c) ourongxing,
under the MIT License. See ``NEWSNOW_LICENSE.txt`` in this package.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from src.feature.atom import read_json, write_json

logger = logging.getLogger(__name__)
CACHE_MAX_AGE = timedelta(minutes=30)


class NewsSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewsCandidate:
    source: str
    rank: int
    title: str
    url: str
    published_at: str | None = None
    summary: str = ""
    metric: str = ""


class DirectNewsService:
    """Collect candidates directly and retain a last-known-good disk cache."""

    def __init__(self, cache_path: Path, timeout_seconds: int = 20):
        self.cache_path = cache_path
        self.timeout_seconds = timeout_seconds
        self._cache_lock = threading.RLock()
        self._fetchers: dict[str, Callable[[int], list[NewsCandidate]]] = {
            "bilibili": self._bilibili,
            "douyin": self._douyin,
            "weibo": self._weibo,
            "baidu": self._baidu,
            "steam": self._steam,
        }

    def fetch(self, source: str, limit: int) -> list[NewsCandidate]:
        if limit <= 0:
            return []
        if source not in self._fetchers:
            raise NewsSourceError(f"未知新闻来源：{source}")
        try:
            items = self._fetchers[source](limit)[:limit]
            if not items:
                raise NewsSourceError("平台返回了空列表")
            self._save_cache(source, items)
            return items
        except Exception as exc:
            logger.exception("新闻平台 %s 直采失败", source)
            cached, fetched_at = self._load_cache(source, limit)
            if cached and fetched_at is not None:
                logger.warning(
                    "新闻平台 %s 使用短时缓存：缓存时间=%s，条数=%s，最大缓存年龄=%s分钟",
                    source,
                    fetched_at.astimezone().isoformat(timespec="seconds"),
                    len(cached),
                    int(CACHE_MAX_AGE.total_seconds() // 60),
                )
                return cached
            if isinstance(exc, NewsSourceError):
                raise
            raise NewsSourceError(f"{source} 采集失败：{exc}") from exc

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        return session

    def _bilibili(self, limit: int) -> list[NewsCandidate]:
        items: list[NewsCandidate] = []
        with self._session() as session:
            for page in range(1, 3):
                response = session.get(
                    "https://api.bilibili.com/x/web-interface/popular",
                    params={"pn": page, "ps": 20},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                for item in data.get("data", {}).get("list", []):
                    bvid = str(item.get("bvid") or "")
                    stat = item.get("stat") or {}
                    owner = item.get("owner") or {}
                    items.append(
                        NewsCandidate(
                            "Bilibili热门视频",
                            len(items) + 1,
                            str(item.get("title") or "").strip(),
                            f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/",
                            _unix_time(item.get("pubdate")),
                            str(item.get("desc") or "").strip()[:500],
                            f"UP主：{owner.get('name', '未知')}；播放：{stat.get('view', 0)}；点赞：{stat.get('like', 0)}",
                        )
                    )
                    if len(items) >= limit:
                        break
                if len(items) >= limit:
                    break
        return [item for item in items if item.title]

    def _douyin(self, limit: int) -> list[NewsCandidate]:
        with self._session() as session:
            session.headers["Referer"] = "https://www.douyin.com/"
            session.get("https://login.douyin.com/", timeout=self.timeout_seconds)
            response = session.get(
                "https://www.douyin.com/aweme/v1/web/hot/search/list/",
                params={
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "detail_list": "1",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            rows = response.json().get("data", {}).get("word_list", [])
        result = []
        for index, item in enumerate(rows[:limit], 1):
            item_id = item.get("sentence_id") or item.get("word") or ""
            result.append(
                NewsCandidate(
                    "抖音热点",
                    index,
                    str(item.get("word") or "").strip(),
                    f"https://www.douyin.com/hot/{quote(str(item_id))}",
                    _unix_time(item.get("event_time")),
                    str(item.get("sentence_tag_name") or "").strip(),
                    f"热度：{item.get('hot_value', 0)}",
                )
            )
        return [item for item in result if item.title]

    def _weibo(self, limit: int) -> list[NewsCandidate]:
        with self._session() as session:
            response = session.get(
                "https://s.weibo.com/top/summary",
                params={"cate": "realtimehot"},
                headers={
                    "Referer": "https://s.weibo.com/top/summary?cate=realtimehot",
                    # Anonymous access cookie used by the upstream NewsNow adapter.
                    "Cookie": "SUB=_2AkMWIuNSf8NxqwJRmP8dy2rhaoV2ygrEieKgfhKJJRMxHRl-yT9jqk86tRB6PaLNvQZR6zYUcYVT1zSjoSreQHidcUq7",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            page = response.text
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I)
        result = []
        for row in rows:
            link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', row, re.S | re.I)
            if not link:
                continue
            title = _text(link.group(2))
            if not title or title in {"实时热点", "更多"}:
                continue
            href = html.unescape(link.group(1))
            metric_match = re.search(r'<span[^>]*>(.*?)</span>', row, re.S | re.I)
            result.append(
                NewsCandidate(
                    "微博热搜",
                    len(result) + 1,
                    title,
                    "https://s.weibo.com" + href if href.startswith("/") else href,
                    metric=_text(metric_match.group(1)) if metric_match else "",
                )
            )
            if len(result) >= limit:
                break
        return result

    def _baidu(self, limit: int) -> list[NewsCandidate]:
        with self._session() as session:
            response = session.get(
                "https://top.baidu.com/board",
                params={"tab": "realtime"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            match = re.search(r"<!--s-data:(.*?)-->", response.text, re.S)
        if not match:
            raise NewsSourceError("百度热榜页面结构已变化")
        data = json.loads(match.group(1))
        cards = data.get("data", {}).get("cards", [])
        rows = cards[0].get("content", []) if cards else []
        result = []
        for item in rows:
            if item.get("isTop"):
                continue
            result.append(
                NewsCandidate(
                    "百度热榜",
                    len(result) + 1,
                    str(item.get("word") or item.get("query") or "").strip(),
                    str(item.get("rawUrl") or item.get("url") or "https://top.baidu.com/board?tab=realtime"),
                    summary=str(item.get("desc") or "").strip()[:500],
                    metric=f"热度：{item.get('hotScore', '')}" if item.get("hotScore") else "",
                )
            )
            if len(result) >= limit:
                break
        return [item for item in result if item.title]

    def _steam(self, limit: int) -> list[NewsCandidate]:
        with self._session() as session:
            response = session.get(
                "https://store.steampowered.com/stats/stats/",
                headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            page = response.text
        rows = re.findall(
            r'<tr[^>]*class="[^"]*player_count_row[^"]*"[^>]*>(.*?)</tr>',
            page,
            re.S | re.I,
        )
        result = []
        for row in rows[:limit]:
            link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', row, re.S | re.I)
            counts = re.findall(r'<span[^>]+class="currentServers"[^>]*>(.*?)</span>', row, re.S | re.I)
            if not link:
                continue
            title = _text(link.group(2))
            result.append(
                NewsCandidate(
                    "Steam当前玩家榜",
                    len(result) + 1,
                    title,
                    html.unescape(link.group(1)),
                    metric=f"当前玩家数：{_text(counts[0])}" if counts else "",
                )
            )
        return [item for item in result if item.title]

    def _save_cache(self, source: str, items: list[NewsCandidate]) -> None:
        with self._cache_lock:
            cache = self._read_cache()
            cache[source] = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "items": [asdict(item) for item in items],
            }
            write_json(self.cache_path, cache)

    def _load_cache(
        self, source: str, limit: int
    ) -> tuple[list[NewsCandidate], datetime | None]:
        raw = self._read_cache().get(source, {})
        try:
            fetched_at = datetime.fromisoformat(str(raw.get("fetched_at")))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched_at > CACHE_MAX_AGE:
                logger.warning(
                    "新闻平台 %s 的缓存已过期，不再使用：缓存时间=%s",
                    source,
                    fetched_at.isoformat(timespec="seconds"),
                )
                return [], fetched_at
            items = [NewsCandidate(**item) for item in raw.get("items", [])[:limit]]
            return items, fetched_at
        except (TypeError, ValueError):
            return [], None

    def _read_cache(self) -> dict[str, Any]:
        with self._cache_lock:
            if not self.cache_path.exists():
                return {}
            try:
                data = read_json(self.cache_path)
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}


def _unix_time(value: object) -> str | None:
    try:
        timestamp = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()
