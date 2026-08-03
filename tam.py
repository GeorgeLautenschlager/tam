#!/usr/bin/env python3
"""Run Tam"""

from __future__ import annotations

from pathlib import Path

from theseus.agentic_memory import AgenticMemory
from theseus.ooda_core import OODACore
from theseus.mono_memory import MonoMemory
from theseus.memory_store import MemoryStore
from theseus.model_providers.claude_provider import ClaudeProvider
from theseus.model_providers.lm_studio_provider import LmStudioProvider
from theseus.model_providers.ollama_provider import OllamaProvider
from theseus.stimulus_log import StimulusLog
from theseus.time_observer import TimeObserver
from theseus.tools.recall import RecallTool
from theseus.tools.registry import all_tools
from theseus.tools.web_chat import WebChat
from theseus.web_chat_ui_observer import WebChatUIObserver

constitution = (Path(__file__).parent / "CONSTITUTION.md").read_text()
# PERSONA.md was folded into CONSTITUTION.md (Section 4+) so persona changes go through
# the same PR/ratification process as everything else Tam is entitled to amend.
persona = ""

def main() -> None:
    stimulus_log = StimulusLog(path=str(Path(__file__).parent / "stimulus_log.jsonl"))

    a_mem = AgenticMemory(
        model_providers=[ClaudeProvider(model="claude-opus-5")],
        embedding_providers=[OllamaProvider(model="nomic-embed-text")],
        store=MemoryStore(Path(__file__).parent / "a_mem.jsonl"),
        stimulus_log=stimulus_log,
    )

    # Sized for Claude Sonnet 5, which is what Tam actually runs on: ClaudeProvider is
    # first in the chain and its availability check is just `which claude`, so the local
    # providers below are fallbacks that almost never win. The budget is a fixed ceiling,
    # not something that grows — and the Claude CLI reports no `usage`, so MonoMemory's
    # chars-per-token estimate never calibrates here either and stays on its seed. Both
    # are fine at this size; revisit if the local providers ever become the usual path.
    # window_size stays a backstop, not the control: Tam's events run ~380 tokens each,
    # so the default cap of 200 would bind at ~75k and quietly undercut the budget above.
    context_assembler = MonoMemory(
        stimulus_log=stimulus_log, token_budget=120_000, window_size=1000
    )

    # Recall is a tool now, not something the context assembler does behind Tam's back.
    # It is not in all_tools() because it needs a memory instance to bind to, so it is
    # wired in here — without it Tam has no long-term memory at all.
    recall = RecallTool(a_mem)
    tools = {**all_tools(), recall.name: recall}

    core = OODACore(
        constitution=constitution,
        persona=persona,
        context_assembler=context_assembler,
        model_providers=[
            ClaudeProvider(model="claude-sonnet-5"),
            LmStudioProvider(model="gemma-4-26b-a4b-it-qat"),
            OllamaProvider(model="gemma4:e4b"),
        ],
        tools=tools,
        memory=a_mem,
        stimulus_log=stimulus_log,
        name="Tam"
    )

    web_observer = WebChatUIObserver(
        stimulus_log=stimulus_log,
        orient_chat_message_callback=core.orient_and_wait
    )

    # WebChat needs the observer, the observer needs core.orient_and_wait, and the core
    # needs the tool — so the web-chat "mouth" is wired in after the other three exist.
    web_chat = WebChat(web_observer=web_observer)
    core.tools[web_chat.name] = web_chat

    # Nudges Tam to orient every 15 minutes if anything new has landed in the log since
    # the last wake, so idle periods don't leave external stimuli unattended indefinitely.
    time_observer = TimeObserver(
        stimulus_log, core.try_orient, self_actor=core.name, interval_seconds=15 * 60
    )
    time_observer.start()

    web_observer.serve(host="0.0.0.0", port=1337)


if __name__ == "__main__":
    main()
