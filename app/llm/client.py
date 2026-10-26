"""OpenAI 兼容的 LLM / Embedding 客户端。

设计：不绑定厂商，只用 base_url + model + key 三个配置；
未配置时抛 LLMUnavailable，由上层降级为规则结论，绝不静默发请求。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

import httpx

from app.config.settings import Settings, get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """LLM 调用失败的统一异常。"""


class LLMUnavailable(LLMError):
    """未配置或不可用，调用方应降级。"""


async def chat_text(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
    settings: Settings | None = None,
) -> str:
    """调用外部 LLM，返回纯文本。messages 为 OpenAI 格式。"""
    cfg = settings or get_settings()
    if not cfg.llm_enabled:
        raise LLMUnavailable("未配置 LLM_API_KEY / LLM_BASE_URL")

    payload = {
        "model": model or cfg.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    url = f"{cfg.llm_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=cfg.llm_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"网络错误：{exc}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("响应缺少 choices")
    return str(choices[0].get("message", {}).get("content", "")).strip()


async def embed_texts(
    texts: list[str], settings: Settings | None = None
) -> tuple[list[list[float]], str]:
    """返回 (向量列表, 来源标识)。未配置外部接口时降级为本地哈希向量。"""
    cfg = settings or get_settings()
    if not texts:
        return [], "empty"
    if not cfg.embedding_enabled:
        dim = max(cfg.embedding_dim, 64)
        return [hashing_embedding(t, dim) for t in texts], "local-hash"

    url = f"{cfg.embedding_base_url.rstrip('/')}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg.embedding_api_key:
        headers["Authorization"] = f"Bearer {cfg.embedding_api_key}"
    payload: dict[str, Any] = {"model": cfg.embedding_model, "input": texts}

    try:
        async with httpx.AsyncClient(timeout=cfg.llm_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"网络错误：{exc}") from exc

    items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
    vectors = [list(map(float, item["embedding"])) for item in items]
    if len(vectors) != len(texts):
        raise LLMError(f"向量数量不匹配：{len(vectors)} != {len(texts)}")
    return vectors, f"{cfg.embedding_model}@{cfg.embedding_base_url}"


def hashing_embedding(text: str, dim: int = 64) -> list[float]:
    """确定性哈希向量：无外部依赖的降级方案，保证 RAG 链路可跑通。"""
    vector = [0.0] * dim
    tokens = [t for t in _tokenize(text) if t]
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        for i in range(0, 4):
            idx = digest[i] % dim
            sign = 1.0 if digest[i + 4] % 2 == 0 else -1.0
            vector[idx] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


def _tokenize(text: str) -> Iterable[str]:
    """中英混排的粗粒度切分：中文按双字切，英文按单词。"""
    buffer, words = [], []
    for char in text.lower():
        if "\u4e00" <= char <= "\u9fff":
            if buffer:
                words.append("".join(buffer))
                buffer = []
            words.append(char)
        elif char.isalnum():
            buffer.append(char)
        else:
            if buffer:
                words.append("".join(buffer))
                buffer = []
    if buffer:
        words.append("".join(buffer))

    grams = list(words)
    chinese = [w for w in words if len(w) == 1 and "\u4e00" <= w <= "\u9fff"]
    grams.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return grams


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
