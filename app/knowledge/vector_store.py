"""向量库封装：默认内嵌轻量实现，可切换 Chroma / FAISS。

默认实现零依赖（JSON 持久化 + 余弦相似度），保证骨架在任何环境都能跑；
生产环境设置 KNOWLEDGE_BACKEND=chroma 并在镜像中安装对应依赖即可。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

from app.config.settings import get_settings
from app.llm.client import cosine
from app.utils.logging import get_logger

logger = get_logger(__name__)


class VectorStore(Protocol):
    backend: str

    def upsert(self, item_id: str, vector: list[float], payload: dict[str, Any]) -> None: ...
    def remove(self, item_id: str) -> None: ...
    def search(self, vector: list[float], top_k: int = 3) -> list[tuple[str, float, dict]]: ...
    def count(self) -> int: ...


class SimpleVectorStore:
    """文件持久化的暴力检索实现，适合数千条经验的量级。"""

    backend = "simple"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_settings().vector_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = {k: v for k, v in raw.items() if isinstance(v, dict)}
            logger.info("向量库载入 %d 条经验", len(self._items))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("向量库文件损坏，将以空库启动：%s", exc)
            self._items = {}

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._items, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)

    def upsert(self, item_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        with self._lock:
            self._items[item_id] = {"vector": vector, "payload": payload}
            self._flush()

    def remove(self, item_id: str) -> None:
        with self._lock:
            if self._items.pop(item_id, None) is not None:
                self._flush()

    def search(self, vector: list[float], top_k: int = 3) -> list[tuple[str, float, dict]]:
        scored = [
            (item_id, round(cosine(vector, item["vector"]), 4), item.get("payload", {}))
            for item_id, item in self._items.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def count(self) -> int:
        return len(self._items)


class ChromaVectorStore:  # pragma: no cover - 需要安装 chromadb
    """Chroma 后端：外部向量库，适合经验量上万后替换。"""

    backend = "chroma"

    def __init__(self, path: Path | None = None, collection: str = "experiences") -> None:
        import chromadb  # 延迟导入，避免默认依赖

        cfg = get_settings()
        self._client = chromadb.PersistentClient(path=str(path or cfg.data_dir / "chroma"))
        self._collection = self._client.get_or_create_collection(collection)

    def upsert(self, item_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        self._collection.upsert(ids=[item_id], embeddings=[vector], metadatas=[payload])

    def remove(self, item_id: str) -> None:
        self._collection.delete(ids=[item_id])

    def search(self, vector: list[float], top_k: int = 3) -> list[tuple[str, float, dict]]:
        result = self._collection.query(query_embeddings=[vector], n_results=top_k)
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        return [
            (i, round(1 - d, 4), m or {}) for i, d, m in zip(ids, distances, metas)
        ]

    def count(self) -> int:
        return int(self._collection.count())


def build_vector_store() -> VectorStore:
    cfg = get_settings()
    if cfg.knowledge_backend == "chroma":
        try:
            return ChromaVectorStore()
        except Exception as exc:  # noqa: BLE001 - 依赖缺失时回退
            logger.warning("Chroma 不可用，回退到内嵌实现：%s", exc)
    return SimpleVectorStore()


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = build_vector_store()
    return _store


def reset_vector_store() -> None:
    global _store
    _store = None
