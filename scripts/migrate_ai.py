"""Optional preflight for a stopped bot; startup performs the same protection."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.feature.ai.migrate import migrate
from src.feature.ai.dsh_upgrade import RuntimeUpgrade
from src.feature.ai.storage import Workspace

parser = argparse.ArgumentParser(description='迁移旧AI数据，冲突时停止且不覆盖目标')
parser.add_argument('--root', type=Path, default=Path('data/ai'))
args = parser.parse_args()
result = migrate(args.root)
workspace = Workspace(args.root)
print(json.dumps({**result, 'persona_path': str(workspace.persona_path), 'runtime_upgrade': RuntimeUpgrade(args.root).prepare()}, ensure_ascii=False, indent=2))
