"""Isolated UI preview; never connects to QQ or starts memory/model jobs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from src.admin import create_admin_app
from src.feature.ai.admin import AiAdmin
from src.feature.ai.feature import AiFeature
from src.feature.atom import FeaturePlugin
from src.feature.manager import FeatureManager
from src.master import MasterService

root = Path("data/ai-admin-validation")
feature = AiFeature(root)
feature.config.save({"memory_auto_learn": False})
master = MasterService(root / "master.json")
manager = FeatureManager(root / "features.json", [feature])
app = create_admin_app(manager, master, [FeaturePlugin(feature, AiAdmin(feature))])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=10011)
