import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.feature.atom import read_json, write_json

DEFAULT_REQUIREMENTS = "面向中国大学生，bilibili平台或者抖音平台的娱乐性质为主，偏二次元、偏游戏，重要的时事政治内容也可以加入，或者大学生圈子的有趣新闻也可以"


@dataclass(frozen=True)
class NewsConfigData:
    broadcast_hour: int = 8
    news_count: int = 10
    requirements: str = DEFAULT_REQUIREMENTS


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
            broadcast_hour=_integer(data.get("broadcast_hour"), 0, 23, 8),
            news_count=_integer(data.get("news_count"), 5, 50, 10),
            requirements=str(data.get("requirements") or DEFAULT_REQUIREMENTS).strip(),
        )

    def save(self, broadcast_hour: int, news_count: int, requirements: str) -> NewsConfigData:
        if not 0 <= broadcast_hour <= 23:
            raise ValueError("播报小时必须在0至23之间")
        if not 5 <= news_count <= 50:
            raise ValueError("新闻条数必须在5至50之间")
        requirements = requirements.strip()
        if not requirements:
            raise ValueError("新闻内容需求不能为空")
        value = NewsConfigData(broadcast_hour, news_count, requirements)
        write_json(self.path, asdict(value))
        return value


def _integer(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default
