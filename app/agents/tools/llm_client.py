import os
import time
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """
    Raised when the LLM client fails to get a response.

    Wraps the underlying error with context about what went wrong.
    is_timeout=True means the model was reachable but took too long.
    is_timeout=False means a connection or unexpected error occurred.

    Usage:
        raise LLMClientError("Model timed out", original_error=e, is_timeout=True)
    """
    def __init__(
        self,
        message: str,
        original_error: Exception = None,
        is_timeout: bool = False
    ):
        super().__init__(message)
        self.original_error = original_error
        self.is_timeout = is_timeout


class LLMClient:
    """
    Lightweight wrapper around the local Ollama inference API.

    All LLM calls in the system go through this client.
    Never call the Ollama API directly from tools or agents —
    always use LLMClient so retry, timeout, and error handling
    are consistent across the entire pipeline.

    Configuration (all from .env):
        OLLAMA_BASE_URL      — Ollama server URL (default: http://localhost:11434)
        OLLAMA_MODEL         — Model to use (default: llama3.1:8b)
        LLM_TIMEOUT_SECONDS  — Request timeout in seconds (default: 30)
        LLM_MAX_RETRIES      — Max retries on timeout (default: 2)

    Usage:
        client = LLMClient()
        response = client.complete(
            system_prompt="You are an email classifier...",
            user_prompt="Classify this email: ..."
        )
    """

    def __init__(self, model: Optional[str] = None, provider: Optional[str] = None):
        self._provider = (provider or os.getenv("LLM_PROVIDER", "ollama")).lower()
        self._base_url = os.getenv(
            "OLLAMA_BASE_URL",
            "http://localhost:11434"
        ).rstrip("/")

        self._model = model or os.getenv(
            "OLLAMA_MODEL",
            "gemini-1.5-flash" if self._provider == "gemini" else ("gpt-4o-mini" if self._provider == "openai" else "qwen3:8b")
        )

        self._timeout = float(os.getenv(
            "LLM_TIMEOUT_SECONDS",
            "30"
        ))

        self._max_retries = int(os.getenv(
            "LLM_MAX_RETRIES",
            "2"
        ))

        self._chat_endpoint = f"{self._base_url}/api/chat"

        logger.info(
            f"LLMClient initialized — provider: {self._provider}, model: {self._model}, "
            f"timeout: {self._timeout}s, max_retries: {self._max_retries}"
        )

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
    ) -> str:
        """
        Send a prompt to the configured LLM provider and return the response text.
        """
        provider = self._provider
        req_model = model or self._model

        # ------------------ GEMINI PROVIDER ------------------
        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise LLMClientError("GEMINI_API_KEY environment variable is not set")
            
            # Map model parameter if not a Gemini model
            model_name = req_model if "gemini" in req_model.lower() else "gemini-1.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}]
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                }
            }

            last_error = None
            for attempt in range(self._max_retries + 1):
                try:
                    if attempt > 0:
                        wait_seconds = 2 ** attempt
                        logger.warning(f"Gemini request timed out. Retrying in {wait_seconds}s...")
                        time.sleep(wait_seconds)

                    logger.debug(f"Sending request to Gemini API (attempt {attempt + 1})")
                    response = httpx.post(url, json=payload, timeout=self._timeout)
                    response.raise_for_status()
                    data = response.json()
                    
                    # Extract Gemini content
                    content = data["candidates"][0]["content"]["parts"][0]["text"]
                    return content
                except httpx.TimeoutException as e:
                    last_error = e
                except Exception as e:
                    raise LLMClientError(f"Gemini API request failed: {e}", original_error=e)
            
            raise LLMClientError(f"Gemini request timed out after maximum retries", original_error=last_error, is_timeout=True)

        # ------------------ OPENAI PROVIDER ------------------
        elif provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise LLMClientError("OPENAI_API_KEY environment variable is not set")
            
            # Map model parameter if not an OpenAI model
            model_name = req_model if "gpt" in req_model.lower() else "gpt-4o-mini"
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}"}
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.0,
                "stream": False
            }

            last_error = None
            for attempt in range(self._max_retries + 1):
                try:
                    if attempt > 0:
                        wait_seconds = 2 ** attempt
                        logger.warning(f"OpenAI request timed out. Retrying in {wait_seconds}s...")
                        time.sleep(wait_seconds)

                    logger.debug(f"Sending request to OpenAI API (attempt {attempt + 1})")
                    response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
                    response.raise_for_status()
                    data = response.json()
                    
                    # Extract OpenAI content
                    content = data["choices"][0]["message"]["content"]
                    return content
                except httpx.TimeoutException as e:
                    last_error = e
                except Exception as e:
                    raise LLMClientError(f"OpenAI API request failed: {e}", original_error=e)
            
            raise LLMClientError(f"OpenAI request timed out after maximum retries", original_error=last_error, is_timeout=True)

        # ------------------ OLLAMA PROVIDER (DEFAULT) ------------------
        else:
            payload = {
                "model": req_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            }

            last_error = None

            for attempt in range(self._max_retries + 1):
                try:
                    if attempt > 0:
                        wait_seconds = 2 ** attempt
                        logger.warning(
                            f"LLM request timed out. Retrying in {wait_seconds}s "
                            f"(attempt {attempt + 1}/{self._max_retries + 1})..."
                        )
                        time.sleep(wait_seconds)

                    logger.debug(
                        f"Sending request to LLM — attempt {attempt + 1}/{self._max_retries + 1}"
                    )

                    response = httpx.post(
                        self._chat_endpoint,
                        json=payload,
                        timeout=self._timeout,
                    )

                    response.raise_for_status()
                    data = response.json()
                    content = data["message"]["content"]
                    return content

                except httpx.TimeoutException as e:
                    last_error = e
                except httpx.ConnectError as e:
                    raise LLMClientError(
                        f"Cannot connect to Ollama at {self._chat_endpoint}. Is Ollama running?",
                        original_error=e,
                        is_timeout=False,
                    )
                except httpx.HTTPStatusError as e:
                    raise LLMClientError(
                        f"Ollama returned HTTP {e.response.status_code}: {e.response.text}",
                        original_error=e,
                        is_timeout=False,
                    )
                except KeyError:
                    raise LLMClientError(
                        f"Unexpected response format from Ollama.",
                        original_error=None,
                        is_timeout=False,
                    )
                except Exception as e:
                    raise LLMClientError(
                        f"Unexpected error during LLM call: {e}",
                        original_error=e,
                        is_timeout=False,
                    )

            raise LLMClientError(
                f"LLM request failed after {self._max_retries + 1} attempts",
                original_error=last_error,
                is_timeout=True,
            )

    def chat(self, messages: list[dict], model: Optional[str] = None) -> str:
        """
        Multi-turn completion — the primitive the reasoning_engine's
        Observe/Reason/Act/Reflect/Finalize loop is built on (specs v3 §3).

        `messages` is a list of {"role": "system"|"user"|"assistant", "content": str}
        dicts, already including any prior turns and tool-result summaries
        (as user-role messages, since none of the three providers wired up
        here expose native tool_use blocks through this thin wrapper — the
        loop is implemented at the reasoning_engine level: it asks the model
        for a JSON-encoded next action each turn, executes real Python tool
        functions against that, and appends the result as the next user turn).

        Reuses complete()'s per-provider transport by splitting the first
        system message out and folding everything else into a single
        prompt, which keeps this a thin, provider-agnostic building block
        rather than three more copies of the retry/timeout logic.
        """
        system_prompt = ""
        turns = []
        for m in messages:
            if m["role"] == "system" and not system_prompt:
                system_prompt = m["content"]
            else:
                turns.append(f"[{m['role'].upper()}]\n{m['content']}")

        user_prompt = "\n\n".join(turns)
        return self.complete(system_prompt=system_prompt, user_prompt=user_prompt, model=model)

    def embed(self, text: str, model: Optional[str] = None) -> list[float]:
        """
        Compute a single embedding vector for `text` (specs v3 §5.3, §9.1).

        Used transiently — the caller (embedding_adapter) never persists
        the vector itself, only the running centroid it feeds into.
        """
        provider = self._provider
        embed_model = model or os.getenv(
            "EMBEDDING_MODEL",
            "nomic-embed-text" if provider == "ollama" else (
                "text-embedding-004" if provider == "gemini" else "text-embedding-3-small"
            ),
        )

        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise LLMClientError("GEMINI_API_KEY environment variable is not set")
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{embed_model}:embedContent?key={api_key}"
            )
            payload = {"content": {"parts": [{"text": text}]}}
            try:
                resp = httpx.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                return resp.json()["embedding"]["values"]
            except Exception as e:
                raise LLMClientError(f"Gemini embedding request failed: {e}", original_error=e)

        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise LLMClientError("OPENAI_API_KEY environment variable is not set")
            url = "https://api.openai.com/v1/embeddings"
            headers = {"Authorization": f"Bearer {api_key}"}
            payload = {"model": embed_model, "input": text}
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
            except Exception as e:
                raise LLMClientError(f"OpenAI embedding request failed: {e}", original_error=e)

        # Ollama (default) — requires an embedding-capable model pulled
        # alongside the chat model (e.g. `ollama pull nomic-embed-text`).
        url = f"{self._base_url}/api/embeddings"
        payload = {"model": embed_model, "prompt": text}
        try:
            resp = httpx.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()["embedding"]
        except httpx.ConnectError as e:
            raise LLMClientError(
                f"Cannot connect to Ollama at {url}. Is Ollama running with "
                f"'{embed_model}' pulled?",
                original_error=e,
            )
        except Exception as e:
            raise LLMClientError(f"Ollama embedding request failed: {e}", original_error=e)

    def is_reachable(self) -> bool:
        """
        Check if the configured LLM service is reachable.
        """
        if self._provider == "gemini":
            return bool(os.getenv("GEMINI_API_KEY"))
        elif self._provider == "openai":
            return bool(os.getenv("OPENAI_API_KEY"))
        else:
            try:
                response = httpx.get(self._base_url, timeout=5.0)
                return response.status_code == 200
            except Exception:
                return False
