#!/usr/bin/env python3
"""Run Tam"""

from __future__ import annotations

from pathlib import Path

from theseus.model_providers.ollama_provider import OllamaProvider
from theseus.web_chat_ui_observer import WebChatUIObserver
from theseus.web_chat_ui_effector import WebChatUIEffector
from theseus.model_providers.claude_provider import ClaudeProvider
from theseus.chat_observer import ChatObserver
from theseus.stimulus_log import StimulusLog
from theseus.chat_effector import ChatEffector
from theseus.model_providers.lm_studio_provider import LmStudioProvider
from theseus.cognitive_core import CognitiveCore
from theseus.agentic_memory import AgenticMemory
from theseus.memory_store import MemoryStore

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
        effectors={},
        memory=a_mem,
        stimulus_log=stimulus_log,
        name="Tam"
    )

    web_observer = WebChatUIObserver(
        stimulus_log=stimulus_log,
        orient_chat_message_callback=core.orient
    )

    web_chat_effector = WebChatUIEffector(web_observer=web_observer)
    core.effectors = {web_chat_effector.name: web_chat_effector}

    web_observer.serve(host="0.0.0.0", port=1337)


if __name__ == "__main__":
    main()
