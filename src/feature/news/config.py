import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json

DEFAULT_REQUIREMENTS = "面向中国大学生，bilibili平台或者抖音平台的娱乐性质为主，偏二次元、偏游戏，重要的时事政治内容也可以加入，或者大学生圈子的有趣新闻也可以"


@dataclass(frozen=True)
class NewsConfigData:
    enabled: bool = False
    broadcast_hour: int = 8
    news_count: int = 10
    requirements: str = DEFAULT_REQUIREMENTS
    bilibili_count: int = 20
    douyin_count: int = 20
    weibo_count: int = 20
    baidu_count: int = 15
    steam_count: int = 5

    @property
    def source_counts(self) -> dict[str, int]:
        return {
            "bilibili": self.bilibili_count,
            "douyin": self.douyin_count,
            "weibo": self.weibo_count,
            "baidu": self.baidu_count,
            "steam": self.steam_count,
        }


class NewsConfig:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> NewsConfigData:
        defaults = NewsConfigData()
        if not self.path.exists():
            return defaults
        try:
            data = read_json(self.path)
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(data, dict):
            return defaults
        return NewsConfigData(
            enabled=bool(data.get("enabled", defaults.enabled)),
            broadcast_hour=_integer(data.get("broadcast_hour"), 0, 23, 8),
            news_count=_integer(data.get("news_count"), 5, 50, 10),
            requirements=str(data.get("requirements") or DEFAULT_REQUIREMENTS).strip(),
            bilibili_count=_integer(data.get("bilibili_count"), 0, 30, 20),
            douyin_count=_integer(data.get("douyin_count"), 0, 30, 20),
            weibo_count=_integer(data.get("weibo_count"), 0, 30, 20),
            baidu_count=_integer(data.get("baidu_count"), 0, 30, 15),
            steam_count=_integer(data.get("steam_count"), 0, 30, 5),
        )

    def save(
        self,
        enabled: bool,
        broadcast_hour: int,
        news_count: int,
        requirements: str,
        bilibili_count: int = 20,
        douyin_count: int = 20,
        weibo_count: int = 20,
        baidu_count: int = 15,
        steam_count: int = 5,
    ) -> NewsConfigData:
        if not 0 <= broadcast_hour <= 23:
            raise ValueError("播报小时必须在0至23之间")
        if not 5 <= news_count <= 50:
            raise ValueError("新闻条数必须在5至50之间")
        requirements = requirements.strip()
        if not requirements:
            raise ValueError("新闻内容需求不能为空")
        source_counts = {
            "Bilibili": bilibili_count,
            "抖音": douyin_count,
            "微博": weibo_count,
            "百度": baidu_count,
            "Steam": steam_count,
        }
        for name, count in source_counts.items():
            if not 0 <= count <= 30:
                raise ValueError(f"{name} 候选数必须在0至30之间")
        if sum(source_counts.values()) < news_count:
            raise ValueError("各平台候选数之和不能小于最终播报条数")
        value = NewsConfigData(
            enabled=enabled,
            broadcast_hour=broadcast_hour,
            news_count=news_count,
            requirements=requirements,
            bilibili_count=bilibili_count,
            douyin_count=douyin_count,
            weibo_count=weibo_count,
            baidu_count=baidu_count,
            steam_count=steam_count,
        )
        write_json(self.path, asdict(value))
        return value


def _integer(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default
