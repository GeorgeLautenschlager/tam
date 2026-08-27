#!/usr/bin/env python3
"""Run Tam"""

from __future__ import annotations

import threading
from pathlib import Path

from theseus.agentic_memory import AgenticMemory
from theseus.auto_core import Autocore
from theseus.memory_store import MemoryStore
from theseus.model_providers.claude_provider import ClaudeProvider
from theseus.model_providers.ollama_provider import OllamaProvider
from theseus.tools.recall import RecallTool
from theseus.tools.registry import all_tools
from theseus.tools.web_chat import WebChat
from theseus.web_chat_ui_observer import WebChatUIObserver

HOME = Path(__file__).parent


class TamCore(Autocore):
    """Autocore plus Tam's agentic memory.

    OODACore consolidated memory itself (`memory.form()` at loop termination);
    Autocore has no memory slot, so the same one-consolidation-per-turn cadence
    is restored here at the turn boundary, before sleeping. form() is a no-op
    when nothing new has landed since the store's high-water mark. `memory` is
    attached after construction because AgenticMemory needs the stimulus log
    that Autocore itself creates.
    """

    memory: AgenticMemory

    def _sleep(self) -> bool:
        self.memory.form()
        return super()._sleep()


def main() -> None:
    # Model providers and tick pacing now live in CADENCE.md, and scheduled tasks
    # in SCHEDULE.md — not here. The old TimeObserver 15-minute nudge is subsumed
    # by the cadence tick; unlike the nudge, ticks fire even when nothing new has
    # landed, which is the point: Tam now acts on its own, not only when poked.
    core = TamCore(
        name="Tam",
        home_directory=HOME,
        tools=all_tools(),
    )

    # The token budget follows whichever model won the turn (each CADENCE.md rule
    # declares its own `context 128k`), so this is only the event-count backstop.
    #
    # It was 1000, on the reasoning that the cap should never bind before the token
    # budget does. That was wrong in practice: with `context 800k` on the rule the
    # budget never binds either, so the *entire* log went up on every turn and grew
    # without limit. At 345 events / 150k tokens both Qwen3.8-27B and Gemma-4-31b
    # stopped emitting tool calls — first replying in plain prose (which no observer
    # delivers), then returning nothing at all. Reproduced against the same models:
    # identical prompt and tools at ~3k tokens call tools 3/3; at 150k, 0/2.
    #
    # So the cap is the control after all, and it is set to hold the prompt inside
    # the regime where tool calling actually works. Tam's events run ~440 tokens
    # each, so 60 events is ~26k tokens and still several hours of history.
    core.context_assembler.window_size = 60

    a_mem = AgenticMemory(
        model_providers=[ClaudeProvider(model="claude-opus-5")],
        embedding_providers=[OllamaProvider(model="nomic-embed-text")],
        store=MemoryStore(HOME / "a_mem.jsonl"),
        stimulus_log=core.stimulus_log,
    )
    core.memory = a_mem

    # Recall is a tool, not something the context assembler does behind Tam's
    # back. It is not in all_tools() because it needs a memory instance to bind
    # to, so it is wired in here — without it Tam has no long-term memory at all.
    recall = RecallTool(a_mem)
    core.tools[recall.name] = recall

    # In front of an Autocore the observer has no cycle to drive: the core hears
    # the appended chat message through its own StimulusLog subscription and cuts
    # its sleep short itself, so `core.wake` here is just an idempotent nudge.
    web_observer = WebChatUIObserver(
        stimulus_log=core.stimulus_log,
        orient_chat_message_callback=core.wake,
    )
    web_chat = WebChat(web_observer=web_observer)
    core.tools[web_chat.name] = web_chat

    # The core loops on its own thread; uvicorn keeps the main thread so Ctrl+C
    # still lands on its signal handlers.
    threading.Thread(target=core.loop, name="tam-core-loop", daemon=True).start()
    web_observer.serve(host="0.0.0.0", port=1337)


if __name__ == "__main__":
    main()
