from __future__ import annotations

import logging
import re
import threading

from .storage import digest

logger = logging.getLogger(__name__)


def entries(text: str) -> list[dict]:
    """Index complete list entries/paragraphs; preserve heading context and source lines."""
    result, headings, block = [], [], []
    start = 0

    def flush():
        if block:
            result.append({"line_start": start, "content": "\n".join(block), "heading": " / ".join(headings)})
            block.clear()

    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            headings[:] = headings[:level - 1] + [line.lstrip("# ")]
        elif line.startswith("- "):
            flush()
            start = number
            block.append(line)
        elif line.strip():
            if not block:
                start = number
            block.append(line)
        else:
            flush()
    flush()
    return result


class VectorIndex:
    def __init__(self, workspace, config):
        self.workspace, self.config = workspace, config
        self._lock = threading.RLock()
        self._model = self._client = None
        self._model_name = ""
        self.last_error = ""

    def _initialize(self):
        settings = self.config.load()
        if self._model is None or settings.embedding_model != self._model_name:
            from sentence_transformers import SentenceTransformer
            import chromadb
            self._model = SentenceTransformer(settings.embedding_model, device="cpu", cache_folder=str(self.workspace.root / ".vector_cache/models"), trust_remote_code=False)
            self._model_name = settings.embedding_model
            self._client = chromadb.PersistentClient(path=str(self.workspace.root / ".vector_cache/chroma"))
        return self._client

    def _collection(self, scope: str):
        client = self._initialize()
        # Model-specific collections avoid mixing incompatible vector dimensions.
        name = scope + "_index_" + digest(self._model_name)[:12]
        return client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"}, embedding_function=None)

    def _encode(self, texts: list[str], query: bool = False):
        self._initialize()
        if query:
            texts = ["为这个句子生成表示以用于检索相关文章：" + text for text in texts]
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def sync(self, relative: str | None = None) -> bool:
        if not self.config.load().vector_index_enabled:
            return False
        with self._lock:
            try:
                state = self.workspace.load_state("index", {})
                rebuild = state.get("dirty", True) or state.get("model") != self.config.load().embedding_model
                paths = self.workspace.files() if relative is None or rebuild else [relative]
                hashes = {} if rebuild else state.get("hashes", {})
                if rebuild:
                    for scope in ("user", "common"):
                        collection = self._collection(scope)
                        all_ids = collection.get()["ids"]
                        if all_ids:
                            collection.delete(ids=all_ids)
                for path in paths:
                    text = self.workspace.read_memory(path)
                    if hashes.get(path) == digest(text):
                        continue
                    rows = entries(text)
                    documents, metadatas, ids = [], [], []
                    for item in rows:
                        # BGE's 512-token input: overlapping slices locate a full entry.
                        for offset in range(0, max(1, len(item["content"])), 300):
                            documents.append(item["heading"][:80] + "\n" + item["content"][offset:offset + 380])
                            metadatas.append({"source_file": path, "line_start": item["line_start"], "qq": path.split("/")[1] if path.startswith("user/") else "", "file_hash": digest(text), "entry_type": path.rsplit("/", 1)[-1].removesuffix(".md"), "last_updated": (re.findall(r"\d{4}-\d{2}-\d{2}", item["content"]) or [""])[-1]})
                            ids.append(digest(f"{path}:{item['line_start']}:{offset}"))
                    vectors = self._encode(documents) if documents else []
                    collection = self._collection(path.split("/")[0])
                    collection.delete(where={"source_file": path})
                    if documents:
                        collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=vectors)
                    hashes[path] = digest(text)
                self.workspace.save_state("index", {"dirty": False, "model": self._model_name, "hashes": hashes})
                self.last_error = ""
                return True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: 向量索引不可用，暂用文本检索"
                self.workspace.save_state("index", {"dirty": True})
                logger.exception("同步向量索引失败；下次同步将全量重建")
                return False

    def search(self, query: str, scope: str = "auto", qq: str | None = None, limit: int = 10) -> list[dict]:
        with self._lock:
            if self.config.load().vector_index_enabled:
                try:
                    state = self.workspace.load_state("index", {})
                    if state.get("dirty", True) or state.get("model") != self.config.load().embedding_model:
                        if not self.sync():
                            return self._lexical(query, scope, qq, limit)
                    vector = self._encode([query], query=True)
                    hits = []
                    for area in ("user", "common") if scope == "auto" else (scope,):
                        collection = self._collection(area)
                        if not collection.count():
                            continue
                        kwargs = {"where": {"qq": qq}} if area == "user" and qq else {}
                        result = collection.query(query_embeddings=vector, n_results=min(limit, collection.count()), **kwargs)
                        for metadata, distance in zip(result["metadatas"][0], result["distances"][0]):
                            path = metadata["source_file"]
                            text = self.workspace.read_memory(path)
                            # Never trust stale cached text, even if a user edited outside WebUI.
                            if digest(text) != metadata["file_hash"]:
                                self.workspace.save_state("index", {"dirty": True})
                                continue
                            item = next((entry for entry in entries(text) if entry["line_start"] == metadata["line_start"]), None)
                            if item:
                                hits.append({"source": path, "content": item["content"], "relevance": max(0.0, 1.0 - distance)})
                    return self._dedup(hits, limit)
                except Exception:
                    self.last_error = "向量检索失败，暂用文本检索"
                    self.workspace.save_state("index", {"dirty": True})
                    logger.exception(self.last_error)
            return self._lexical(query, scope, qq, limit)

    @staticmethod
    def _dedup(hits: list[dict], limit: int):
        seen, result = set(), []
        for hit in sorted(hits, key=lambda h: h["relevance"], reverse=True):
            key = (hit["source"], hit["content"])
            if key not in seen:
                seen.add(key)
                result.append(hit)
        return result[:limit]

    def _lexical(self, query, scope, qq, limit):
        terms = set(re.findall(r"[\u3400-\u9fff]|[a-z0-9_]+", query.casefold()))
        hits = []
        for path in self.workspace.files():
            if scope != "auto" and not path.startswith(scope + "/"):
                continue
            if qq and path.startswith("user/") and not path.startswith(f"user/{qq}/"):
                continue
            for item in entries(self.workspace.read_memory(path)):
                overlap = sum(term in item["content"].casefold() for term in terms)
                if overlap:
                    hits.append({"source": path, "content": item["content"], "relevance": overlap / max(1, len(terms))})
        return self._dedup(hits, limit)

    def similar_pairs(self, path: str, threshold: float = 0.8) -> list[tuple[str, str]]:
        with self._lock:
            rows = entries(self.workspace.read_memory(path))
            if len(rows) < 2 or not self.config.load().vector_index_enabled:
                return []
            vectors = self._encode([row["content"][:400] for row in rows])
            pairs = []
            for i, left in enumerate(vectors):
                for j in range(i + 1, len(vectors)):
                    if sum(a * b for a, b in zip(left, vectors[j])) > threshold:
                        pairs.append((rows[i]["content"], rows[j]["content"]))
                        if len(pairs) >= 50:
                            return pairs
            return pairs
