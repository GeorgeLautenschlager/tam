#!/usr/bin/env python3
"""
tam-web.py — Local web chat interface for Tam

Serves a simple browser-based chat UI on the LAN. Works without internet —
the Claude Code CLI runs locally, and the web server has no external dependencies.

Usage:
    python3 tam-web.py

    Or as a systemd service:
        systemctl --user start tam-web

Config (via .env or environment):
    TAM_WEB_PORT=8080      Port to listen on (default: 8080)
    TAM_HOME=~/tam         Tam's home directory
    TAM_MAX_TURNS=10       Max agent turns per message
    TAM_TIMEOUT=300        Seconds before giving up on Claude
"""

import json
import logging
import os
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
SOUL_PATH = TAM_HOME / "SOUL.md"
MAX_TURNS = int(os.environ.get("TAM_MAX_TURNS", "10"))
TIMEOUT_SECONDS = int(os.environ.get("TAM_TIMEOUT", "300"))
PORT = int(os.environ.get("TAM_WEB_PORT", "8080"))

# ── Logging ──────────────────────────────────────────────────────────────────

(TAM_HOME / "logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(TAM_HOME / "logs" / "web.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tam-web")

# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tam</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #1a1a1a;
    color: #e8e8e8;
    height: 100dvh;
    display: flex;
    flex-direction: column;
  }

  header {
    padding: 12px 16px;
    background: #111;
    border-bottom: 1px solid #2a2a2a;
    font-size: 15px;
    font-weight: 600;
    color: #aaa;
    letter-spacing: 0.05em;
  }

  #log {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .bubble {
    max-width: 80%;
    padding: 10px 14px;
    border-radius: 18px;
    line-height: 1.5;
    font-size: 15px;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .bubble.you {
    align-self: flex-end;
    background: #2563eb;
    color: #fff;
    border-bottom-right-radius: 4px;
  }

  .bubble.tam {
    align-self: flex-start;
    background: #2a2a2a;
    color: #e8e8e8;
    border-bottom-left-radius: 4px;
  }

  .bubble.thinking {
    align-self: flex-start;
    background: #222;
    color: #666;
    font-style: italic;
    border-bottom-left-radius: 4px;
  }

  #bar {
    display: flex;
    gap: 8px;
    padding: 12px 16px;
    background: #111;
    border-top: 1px solid #2a2a2a;
  }

  #msg {
    flex: 1;
    padding: 10px 14px;
    border-radius: 22px;
    border: 1px solid #333;
    background: #222;
    color: #e8e8e8;
    font-size: 15px;
    outline: none;
    resize: none;
    max-height: 120px;
    overflow-y: auto;
  }

  #msg:focus { border-color: #2563eb; }

  #send {
    padding: 10px 18px;
    border-radius: 22px;
    border: none;
    background: #2563eb;
    color: #fff;
    font-size: 15px;
    cursor: pointer;
    white-space: nowrap;
    align-self: flex-end;
  }

  #send:disabled { background: #333; color: #666; cursor: default; }
  #send:hover:not(:disabled) { background: #1d4ed8; }
</style>
</head>
<body>
<header>Tam</header>
<div id="log"></div>
<div id="bar">
  <textarea id="msg" placeholder="Message Tam…" rows="1"></textarea>
  <button id="send">Send</button>
</div>
<script>
const log  = document.getElementById('log');
const msg  = document.getElementById('msg');
const send = document.getElementById('send');

function addBubble(text, cls) {
  const b = document.createElement('div');
  b.className = 'bubble ' + cls;
  b.textContent = text;
  log.appendChild(b);
  log.scrollTop = log.scrollHeight;
  return b;
}

async function submit() {
  const text = msg.value.trim();
  if (!text) return;

  msg.value = '';
  msg.style.height = '';
  send.disabled = true;

  addBubble(text, 'you');
  const thinking = addBubble('Thinking…', 'thinking');

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    thinking.remove();
    addBubble(data.response || data.error || '(no response)', res.ok ? 'tam' : 'thinking');
  } catch (e) {
    thinking.remove();
    addBubble('Network error — is the server running?', 'thinking');
  } finally {
    send.disabled = false;
    msg.focus();
  }
}

send.addEventListener('click', submit);

msg.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});

// Auto-grow textarea
msg.addEventListener('input', () => {
  msg.style.height = '';
  msg.style.height = Math.min(msg.scrollHeight, 120) + 'px';
});

msg.focus();
</script>
</body>
</html>"""

# ── Claude invocation ─────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    soul = SOUL_PATH.read_text() if SOUL_PATH.exists() else ""
    return f"""{soul}

You are in web chat mode. George is messaging you via the local web interface.
Keep responses clear and readable in plain text — no Discord-specific formatting needed.
Read MEMORY.md and STATE.md if you need context, but use judgment about when it's
worth the tokens. Update STATE.md or MEMORY.md if George tells you something worth
remembering."""


def ask_tam(message: str) -> str:
    """Invoke Claude Code headless with Tam's identity. Blocking."""
    system_prompt = load_system_prompt()

    cmd = [
        "claude", "-p", message,
        "--append-system-prompt", system_prompt,
        "--allowedTools", "Read,Write",
        "--dangerously-skip-permissions",
        "--max-turns", str(MAX_TURNS),
        "--output-format", "json",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            cwd=str(TAM_HOME),
        )

        if proc.returncode != 0:
            log.error(f"Claude exited {proc.returncode}: {proc.stderr.decode()}")
            return "Sorry, I hit an error on that one. Check the logs."

        result = json.loads(proc.stdout.decode())
        text = result.get("result", "No response generated.")
        cost = result.get("total_cost_usd", "unknown")

        log.info(f"Response generated. Cost: ${cost}")
        log_usage(cost, result.get("usage", {}))

        return text

    except subprocess.TimeoutExpired:
        log.error("Claude Code timed out")
        return "I timed out on that one. Try a simpler question or try again."
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude output: {e}")
        return "Got a garbled response. Check the logs."
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return f"Something went wrong: {e}"


def log_usage(cost: float, usage: dict) -> None:
    ledger = TAM_HOME / "USAGE.log"
    timestamp = datetime.now().isoformat()
    entry = f"{timestamp} | web | cost=${cost} | tokens={json.dumps(usage)}\n"
    with open(ledger, "a") as f:
        f.write(entry)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/chat":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            data = json.loads(raw)
            message = data.get("message", "").strip()
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        if not message:
            self._json_response(400, {"error": "Empty message"})
            return

        log.info(f"Chat message: {message[:100]}{'...' if len(message) > 100 else ''}")
        response = ask_tam(message)
        self._json_response(200, {"response": response})

    def _json_response(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so one slow Claude call
    doesn't block the server from accepting new connections."""
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"Tam web chat running on http://0.0.0.0:{PORT}  (TAM_HOME={TAM_HOME})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
