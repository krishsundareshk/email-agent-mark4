"""
embedding_adapter — wraps whichever LLM provider handles embeddings
(specs v3 §5.3, §9.1).

May be a different provider than the one handling reasoning: swapping
LLM_PROVIDER (e.g. moving Stage 1 from Ollama to OpenAI) should not
silently reset or degrade the combined distribution's semantic space,
since that distribution is built from centroids computed with whatever
embedding provider was active at the time. This module pins embeddings
to EMBEDDING_PROVIDER (falls back to LLM_PROVIDER if unset) so that
choice is explicit and documented, not implicit in whatever
LLM_PROVIDER happens to be set to on a given day.
"""
import os
import logging
from typing import Optional

from app.agents.tools.llm_client import LLMClient, LLMClientError

logger = logging.getLogger(__name__)


class EmbeddingAdapter:
    """
    embed(text) -> vector. Transient use only — the caller must never
    persist the returned vector itself, only the running centroid it
    feeds into (see sender_memory_store.update_label_centroid).
    """

    def __init__(self):
        provider = os.getenv("EMBEDDING_PROVIDER", os.getenv("LLM_PROVIDER", "ollama"))
        self._client = LLMClient(provider=provider)
        self._provider = provider.lower()

    def embed(self, text: str) -> Optional[list[float]]:
        if not text or not text.strip():
            return None
        try:
            return self._client.embed(text)
        except LLMClientError as e:
            logger.warning(f"Embedding computation failed, skipping: {e}")
            return None

    @property
    def provider(self) -> str:
        return self._provider
