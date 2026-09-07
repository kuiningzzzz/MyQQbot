"""Download/cache the local model and verify real semantic retrieval on synthetic data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chromadb
from sentence_transformers import SentenceTransformer

from src.feature.ai.config import Config
from src.feature.ai.index import VectorIndex
from src.feature.ai.storage import Workspace


def main():
    cache = Path("data/ai/.vector_cache/models").resolve()
    model_name = "BAAI/bge-small-zh-v1.5"
    model = SentenceTransformer(model_name, device="cpu", cache_folder=str(cache), trust_remote_code=False)
    # Chroma holds file handles on Windows, so use a persistent ignored test workspace.
    root = Path("data/ai-vector-validation")
    workspace = Workspace(root)
    config = Config(root)
    config.save({"embedding_model": model_name})
    index = VectorIndex(workspace, config)
    index._model, index._model_name = model, model_name
    index._client = chromadb.PersistentClient(path=str(workspace.root / ".vector_cache/chroma"))
    workspace.write_memory("user/123/PREFERENCE.md", "# 食物\n- 吃花生会过敏 | 更新时间: 2026-09-07\n- 喜欢玩赛车游戏 | 更新时间: 2026-09-07\n")
    workspace.write_memory("user/456/PREFERENCE.md", "- 喜欢咖啡 | 更新时间: 2026-09-07\n")
    workspace.save_state("index", {"dirty": True})
    assert index.sync(), index.last_error
    result = index.search("有什么食物不能吃", "user", "123")
    assert result and "花生" in result[0]["content"], result
    assert all(item["source"].startswith("user/123/") for item in result)
    workspace.write_memory("user/123/PREFERENCE.md", "- 对牛奶过敏 | 更新时间: 2026-09-07\n")
    assert index.sync("user/123/PREFERENCE.md")
    result = index.search("食物过敏", "user", "123")
    assert len(result) == 1 and "牛奶" in result[0]["content"], result
    print("PASS: real BGE embeddings, Chroma retrieval, QQ filter, stale-entry replacement")
    print("Model cache:", cache)


if __name__ == "__main__":
    main()
