# Cadence

Which model Tam thinks with at which time of day, and how often to take an
autonomous turn. Windows are matched against server time and the first matching
window wins; `start == end` covers the whole day, so the two whole-day windows
below are really a priority chain — Claude first, LM Studio when Claude isn't
reachable — and the `default` line is the last-resort fallback (Ollama). This
reproduces the provider chain that used to be hard-coded in tam.py.

- 00:00-00:00: claude claude-sonnet-5, tick every 15 minutes
- 00:00-00:00: lm_studio gemma-4-26b-a4b-it-qat, tick every 15 minutes
- default: ollama gemma4:e4b, tick every 15 minutes
