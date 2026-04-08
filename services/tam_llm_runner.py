"""
tam-llm-runner.py — Local LLM invocation runner.

Provides a unified interface with fallback chain:
    LM Studio → Ollama → canned response

"""

import asyncio
import logging
from typing import Any, Optional, Union

from tam_llm_adapters import LmStudioClient, OllamaClient, _uuid


log = logging.getLogger("tam-llm-runner")


# ── Canned Response ───────────────────────────────────────────────────────────

LOCAL_MODE_CANONICAL_RESPONSE = (
    "I'm trying to run in local mode but I can't access LM Studio or Ollama. Send help."
)


# ── Main Runner Class ──────────────────────────────────────────────────────────

class LocalLlmRunner:
    """Runs local LLM invocations with fallback chain."""

    def __init__(self, lmstudio_url: str = "http://192.168.50.216:1234/v1", ollama_url: str = "http://localhost:11434"):
        self._lmstudio_url = lmstudio_url
        self._ollama_url = ollama_url

    async def run(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """Run a chat completion with fallback chain.

        Args:
            model: Model name to use (e.g., "opus", "sonnet", "haiku").
                   Passed through to the service without mapping.
            messages: Chat history in OpenAI-compatible format.
            tools: Optional list of function definitions for tool calling.

        Returns:
            dict with {result, cost, session_id, exit_code}
        """
        log.info(f"Local LLM run requested: model={model}, len(messages)={len(messages)}")

        # Try LM Studio first
        try:
            result = await self._try_lmstudio(model, messages, tools)
            if result["exit_code"] == 0:
                log.info(f"Local LLM succeeded on LM Studio (model={model})")
                return result
        except Exception as e:
            log.warning(f"Local LLM failed on LM Studio: {e}")

        # Try Ollama next
        try:
            result = await self._try_ollama(model, messages, tools)
            if result["exit_code"] == 0:
                log.info(f"Local LLM succeeded on Ollama (model={model})")
                return result
        except Exception as e:
            log.warning(f"Local LLM failed on Ollama: {e}")

        # Fallback to canned response
        log.info("Local LLM fallback: all services unavailable, returning canned response")
        return {
            "result": LOCAL_MODE_CANONICAL_RESPONSE,
            "cost": 0.0,
            "session_id": str(_uuid()),
            "exit_code": -1,
        }

    async def _try_lmstudio(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]],
    ) -> dict[str, Any]:
        """Try LM Studio endpoint."""
        client = LmStudioClient(self._lmstudio_url)

        try:
            return await client.chat(model=model, messages=messages, tools=tools)
        except Exception as e:
            log.error(f"LmStudio exception: {type(e).__name__}: {e}")
            raise

    async def _try_ollama(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]],
    ) -> dict[str, Any]:
        """Try Ollama endpoint."""
        client = OllamaClient(self._ollama_url)

        try:
            return await client.chat(model=model, messages=messages, tools=tools)
        except Exception as e:
            log.error(f"Ollama exception: {type(e).__name__}: {e}")
            raise


# ── Convenience Function (synchronous wrapper for supervisor.py) ───────────────

def run_local(
    prompt: str,
    model: str = "sonnet",
    system_prompt: str = "",
    tools: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Convenience function to invoke local LLM (sync wrapper for async runner).

    This is called directly from supervisor.py without awaiting.
    Converts the prompt to OpenAI-style messages format.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        runner = LocalLlmRunner()
        result = {
            "result": "[error: internal state unavailable]",
            "cost": 0.0,
            "session_id": "",
            "exit_code": -1,
        }

        def run_in_loop():
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            try:
                result["result"], cost, session_id, exit_code = loop.run_until_complete(
                    runner.run(model, messages, tools)
                )
                result["cost"] = cost
                result["session_id"] = session_id
                result["exit_code"] = exit_code
            except Exception as e:
                log.exception(f"run_local exception: {e}")
                result["result"] = f"[error: {e}]"
                result["exit_code"] = -1

        try:
            loop.run_until_complete(run_in_loop())
        finally:
            loop.close()

        return result

    except Exception as e:
        log.exception(f"run_local wrapper exception: {e}")
        return {
            "result": LOCAL_MODE_CANONICAL_RESPONSE,
            "cost": 0.0,
            "session_id": "",
            "exit_code": -1,
        }
