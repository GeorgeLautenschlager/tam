"""
tam-llm-adapters.py — Local LLM invocation adapters for LM Studio and Ollama.

Provides JSON-RPC clients for tool calls, model resolution, and response parsing.
Both services use HTTP APIs (JSON-RPC over localhost).
"""

import json
import logging
from typing import Any, Optional

from httpx import AsyncClient, Timeout, HTTPError


# ── Constants ──────────────────────────────────────────────────────────────────

LM_STUDIO_URL = "http://localhost:1234/v1"
OLLAMA_URL = "http://localhost:11434/api/chat"

LM_STUDIO_TIMEOUT = 300  # seconds
OLLAMA_STREAM_TIMEOUT = 600  # seconds for streaming responses

log = logging.getLogger("tam-llm-adapters")


# ── LM Studio Client ──────────────────────────────────────────────────────────

class LmStudioClient:
    """JSON-RPC client for LM Studio (uses OpenAI-compatible API)."""

    def __init__(self, url: str = LM_STUDIO_URL):
        self._url = url.rstrip("/")
        self._client = AsyncClient()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def chat(
        self,
        model: Optional[str] = None,
        messages: list[dict] = None,
        tools: list[dict] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Invoke chat completion via LM Studio's OpenAI-compatible API."""
        url = f"{self._url}/chat/completions"

        payload = {
            "model": model or "",
            "messages": messages or [],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        log.debug(f"LmStudio chat: {payload['model'][:40]}")

        try:
            async with self._client.post(url, json=payload) as resp:
                data = await resp.json()
                return {
                    "result": self._extract_response(data),
                    "cost": 0.0,
                    "session_id": str(self._uuid()),
                    "exit_code": 0,
                }
        except HTTPError as e:
            log.error(f"LmStudio chat failed: {e}")
            raise
        except json.JSONDecodeError as e:
            log.error(f"LmStudio response invalid JSON: {e}")
            raise

    def _extract_response(self, data: dict[str, Any]) -> str:
        """Extract chat text from LM Studio's OpenAI-compatible response."""
        choices = data.get("choices", [])
        if not choices:
            return ""
        first = choices[0]
        message = first.get("message", {})
        content = message.get("content", "")
        if not content:
            # Check for tool_calls (function calling)
            tool_calls = first.get("message", {}).get("tool_calls", [])
            if tool_calls:
                return json.dumps(tool_calls, indent=2)
        return content

    async def acall_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool call (function/call)."""
        # For local mode, we just return the arguments as-is.
        # In production, this would invoke subprocess or another service.
        log.debug(f"Tool call: {name}({arguments})")
        return {"output": json.dumps(arguments)}


# ── Ollama Client ─────────────────────────────────────────────────────────────

class OllamaClient:
    """HTTP client for Ollama (native streaming support)."""

    def __init__(self, url: str = OLLAMA_URL):
        self._url = url.rstrip("/")
        self._client = AsyncClient()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def chat(
        self,
        model: Optional[str] = None,
        messages: list[dict] = None,
        tools: list[dict] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Invoke chat completion via Ollama's /api/chat endpoint."""
        url = f"{self._url}/api/chat"

        payload = {
            "model": model or "",
            "messages": messages or [],
            "temperature": temperature,
            "stream": False,  # Non-streaming for simplicity
        }
        if tools:
            payload["tools"] = tools

        log.debug(f"Ollama chat: {payload['model'][:40]}")

        try:
            async with self._client.post(url, json=payload) as resp:
                data = await resp.json()
                return {
                    "result": data.get("message", {}).get("content", ""),
                    "cost": 0.0,
                    "session_id": str(self._uuid()),
                    "exit_code": 0,
                }
        except HTTPError as e:
            log.error(f"Ollama chat failed: {e}")
            raise
        except json.JSONDecodeError as e:
            log.error(f"Ollama response invalid JSON: {e}")
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    """Generate a simple UUID-like string."""
    import uuid
    return str(uuid.uuid4())[:8]
