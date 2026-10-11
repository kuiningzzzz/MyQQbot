"""Isolated UI preview on localhost:10012; never connects to QQ or models."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from src.admin import create_admin_app
from src.admin_auth import AdminAuth
from src.feature.ai.feature import AiFeature
from src.feature.atom import FeaturePlugin
from src.feature.manager import FeatureManager
from src.feature.timing.feature import TimingFeature
from src.feature.timing.admin import TimingAdmin
from src.master import MasterService

root = Path('data/timing-admin-validation')
ai = AiFeature(root / 'ai')
ai.config.save({'memory_auto_learn':False, 'vector_index_enabled':False})
master = MasterService(root / 'master.json')
feature = TimingFeature(ai, master, root / 'tasks.sqlite3')
manager = FeatureManager(root / 'features.json', [feature])
with patch('src.admin.AdminAuth', return_value=AdminAuth(root / 'preview-auth.json')):
    app = create_admin_app(manager, master, [FeaturePlugin(feature, TimingAdmin(feature))])

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=10012)
