from __future__ import annotations

import logging
import re
import threading
import time

from .storage import digest

logger = logging.getLogger(__name__)


class _ModelUnavailable(RuntimeError):
    """Embedding 模型未能加载；向量索引降级为纯文本检索。"""


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
    # 模型加载失败后，至少间隔多久才允许重试一次（避免每次聊天都触发下载风暴）。
    MODEL_RETRY_SECONDS = 300

    def __init__(self, workspace, config):
        self.workspace, self.config = workspace, config
        self._lock = threading.RLock()
        self._model = self._client = None
        self._model_name = ""
        self.last_error = ""
        self._model_error = ""
        self._model_failed_at = 0.0
        self._degraded_logged = 0.0

    def _initialize(self):
        settings = self.config.load()
        if self._model is None or settings.embedding_model != self._model_name:
            if self._model_error and time.time() - self._model_failed_at < self.MODEL_RETRY_SECONDS:
                raise _ModelUnavailable(f"embedding 模型暂不可用：{self._model_error}")
            try:
                from sentence_transformers import SentenceTransformer
                import chromadb
                model = SentenceTransformer(settings.embedding_model, device="cpu", cache_folder=str(self.workspace.data_root / ".vector_cache/models"), trust_remote_code=False)
                client = chromadb.PersistentClient(path=str(self.workspace.data_root / ".vector_cache/chroma"))
                recovered = bool(self._model_error)
                self._model, self._client, self._model_name = model, client, settings.embedding_model
                self._model_error = ""
                self._model_failed_at = 0.0
                if recovered:
                    logger.warning("embedding 模型已成功加载，向量索引恢复正常：%s", settings.embedding_model)
            except Exception as exc:
                # 不保留半初始化状态；下次重试窗口内直接快速失败，不联网。
                self._model = self._client = None
                self._model_name = ""
                self._model_error = f"{settings.embedding_model} 加载失败（{type(exc).__name__}: {exc}）"
                self._model_failed_at = time.time()
                raise _ModelUnavailable(self._model_error) from exc
        return self._client

    def preload(self) -> tuple[bool, str]:
        """启动时显式加载模型，避免聊天进行到一半才联网下载/失败。"""
        try:
            self._initialize()
            return True, ""
        except _ModelUnavailable as exc:
            self._degraded(f"{exc}；向量检索已降级为纯文本检索，每 {self.MODEL_RETRY_SECONDS} 秒自动重试一次模型")
            return False, str(exc)

    def _degraded(self, note: str):
        """降级提示只会在每次重试窗口出现一次（WARNING），避免日志风暴。"""
        self.last_error = note
        now = time.time()
        if now - self._degraded_logged >= self.MODEL_RETRY_SECONDS:
            self._degraded_logged = now
            logger.warning("向量索引降级：%s", note)
        else:
            logger.debug("向量索引仍不可用（降级中）：%s", note)

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
                    for scope in ("user", "group", "common"):
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
                            metadatas.append({"source_file": path, "line_start": item["line_start"], "qq": path.split("/")[1] if path.startswith("user/") else "", "group_id": path.split("/")[1] if path.startswith("group/") else "", "file_hash": digest(text), "entry_type": path.rsplit("/", 1)[-1].removesuffix(".md"), "last_updated": (re.findall(r"\d{4}-\d{2}-\d{2}", item["content"]) or [""])[-1]})
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
            except _ModelUnavailable as exc:
                # 模型不可用时不必反复标记脏索引：索引文件没有变化，只做提示并保持文本检索。
                self._degraded(f"{exc}；本次同步跳过，继续使用纯文本检索")
                return False
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: 向量索引不可用，暂用文本检索"
                self.workspace.save_state("index", {"dirty": True})
                logger.exception("同步向量索引失败；下次同步将全量重建")
                return False

    def search(self, query: str, scope: str = "auto", qq: str | None = None, limit: int = 10, group_id: str | None = None) -> list[dict]:
        with self._lock:
            if self.config.load().vector_index_enabled:
                try:
                    state = self.workspace.load_state("index", {})
                    if state.get("hashes", {}) != {p: digest(self.workspace.read_memory(p)) for p in self.workspace.files()}:
                        state["dirty"] = True
                        self.workspace.save_state("index", state)
                    if state.get("dirty", True) or state.get("model") != self.config.load().embedding_model:
                        if not self.sync():
                            return self._lexical(query, scope, qq, limit, group_id)
                    vector = self._encode([query], query=True)
                    hits = []
                    for area in ("user", "group", "common") if scope == "auto" else (scope,):
                        collection = self._collection(area)
                        if not collection.count():
                            continue
                        kwargs = {"where": {"qq": qq}} if area == "user" and qq else {}
                        if area == "group" and group_id:
                            kwargs = {"where": {"group_id": group_id}}
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
                except _ModelUnavailable as exc:
                    self._degraded(f"{exc}；本次查询已降级为纯文本检索")
                except Exception:
                    self.last_error = "向量检索失败，暂用文本检索"
                    self.workspace.save_state("index", {"dirty": True})
                    logger.exception(self.last_error)
            return self._lexical(query, scope, qq, limit, group_id)

    @staticmethod
    def _dedup(hits: list[dict], limit: int):
        seen, result = set(), []
        for hit in sorted(hits, key=lambda h: h["relevance"], reverse=True):
            key = (hit["source"], hit["content"])
            if key not in seen:
                seen.add(key)
                result.append(hit)
        return result[:limit]

    def _lexical(self, query, scope, qq, limit, group_id=None):
        terms = set(re.findall(r"[\u3400-\u9fff]|[a-z0-9_]+", query.casefold()))
        hits = []
        for path in self.workspace.files():
            if scope != "auto" and not path.startswith(scope + "/"):
                continue
            if qq and path.startswith("user/") and not path.startswith(f"user/{qq}/"):
                continue
            if group_id and path.startswith("group/") and not path.startswith(f"group/{group_id}/"):
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
            try:
                vectors = self._encode([row["content"][:400] for row in rows])
            except _ModelUnavailable as exc:
                self._degraded(f"{exc}；碎片整理跳过向量去重")
                return []
            pairs = []
            for i, left in enumerate(vectors):
                for j in range(i + 1, len(vectors)):
                    if sum(a * b for a, b in zip(left, vectors[j])) > threshold:
                        pairs.append((rows[i]["content"], rows[j]["content"]))
                        if len(pairs) >= 50:
                            return pairs
            return pairs
