# Cadence

Which model Tam thinks with at which time of day, and how often to take an
autonomous turn. Windows are matched against server time and the first matching
window wins; `start == end` covers the whole day, so the two whole-day windows
below are really a priority chain — Claude first, LM Studio when Claude isn't
reachable — and the `default` line is the last-resort fallback (Ollama). This
reproduces the provider chain that used to be hard-coded in tam.py.

`context` is that model's context window, and is what my prompt gets sized against.
It belongs on the rule rather than on the provider because it is a property of the
server as loaded: the same GGUF is 128k on the Unsloth box and 4k on a default Ollama.
A rule that omits it falls back to a deliberately small window (4k) rather than
guessing high — guessing high is what overruns.

- 00:00-16:00: unsloth Qwen3.8-27B-GGUF, context 128k, tick every 5 minutes
<!-- - 08:00-16:00: claude claude-sonnet-4-6, context 200k, tick every 15 minutes -->
- 16:00-24:00: claude claude-opus-5-0, context 200k, tick every 30 minutes
- default: ollama gemma4:e4b, context 4k, tick every 60 minutes