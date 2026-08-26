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

    # Tam's ratified identity is CONSTITUTION.md (PERSONA.md was folded into it —
    # see Section 4+ — so persona changes go through the same PR/ratification
    # process as everything else). Autocore reads lowercase constitution.md and
    # persona.md instead; the empty files it touches at boot are inert because the
    # ratified text is loaded over them here. GOALS.md, which used to ride in the
    # persona slot, is now read natively by Autocore every turn.
    core.constitution = (HOME / "CONSTITUTION.md").read_text()

    # The context budget is no longer set here. It was a fixed 120k sized for
    # Claude, and it stayed 120k after CADENCE.md started routing the morning to a
    # 128k local model — which is how Tam ended up prompting past its own window.
    # Each cadence rule now declares its model's window (`context 128k`), so the
    # budget follows whichever model actually won the turn. window_size stays a
    # backstop rather than the control: Tam's events run ~350 tokens each, so the
    # default cap of 200 would bind at ~70k and quietly undercut the real budget.
    core.context_assembler.window_size = 1000

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
