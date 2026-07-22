#!/usr/bin/env python3
"""Run Tam"""

from __future__ import annotations

from pathlib import Path

from theseus.agentic_memory import AgenticMemory
from theseus.cognitive_core import CognitiveCore
from theseus.memory_store import MemoryStore
from theseus.model_providers.claude_provider import ClaudeProvider
from theseus.model_providers.lm_studio_provider import LmStudioProvider
from theseus.model_providers.ollama_provider import OllamaProvider
from theseus.stimulus_log import StimulusLog
from theseus.tools.registry import all_tools
from theseus.tools.web_chat import WebChat
from theseus.web_chat_ui_observer import WebChatUIObserver

constitution = (Path(__file__).parent / "CONSTITUTION.md").read_text()
persona = (Path(__file__).parent / "PERSONA.md").read_text()

def main() -> None:
    stimulus_log = StimulusLog(path=str(Path(__file__).parent / "stimulus_log.jsonl"))

    a_mem = AgenticMemory(
        model_providers=[ClaudeProvider(model="claude-sonnet-5")],
        embedding_providers=[OllamaProvider(model="nomic-embed-text")],
        store=MemoryStore(Path(__file__).parent / "a_mem.jsonl"),
        stimulus_log=stimulus_log,
    )

    core = CognitiveCore(
        constitution=constitution + "\n\n" + persona,
        model_providers=[
            ClaudeProvider(model="claude-sonnet-5"),
            LmStudioProvider(model="gemma-4-26b-a4b-it-qat"),
            OllamaProvider(model="gemma4:e4b"),
        ],
        tools=all_tools(),
        memory=a_mem,
        stimulus_log=stimulus_log,
        name="Tam"
    )

    web_observer = WebChatUIObserver(
        stimulus_log=stimulus_log,
        orient_chat_message_callback=core.orient
    )

    # WebChat needs the observer, the observer needs core.orient, and the core needs
    # the tool — so the web-chat "mouth" is wired in after the other three exist.
    web_chat = WebChat(web_observer=web_observer)
    core.tools[web_chat.name] = web_chat

    web_observer.serve(host="0.0.0.0", port=1337)


if __name__ == "__main__":
    main()
