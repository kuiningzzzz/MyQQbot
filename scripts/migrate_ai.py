"""Optional explicit invocation; AiFeature also runs this at every startup."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.feature.ai.migrate import migrate

parser = argparse.ArgumentParser(description='迁移旧AI数据，冲突时停止且不覆盖目标')
parser.add_argument('--root', type=Path, default=Path('data/ai'))
args = parser.parse_args()
print(json.dumps(migrate(args.root), ensure_ascii=False, indent=2))
