#!/usr/bin/env python3
"""
tam-reflex.py — Local-model orchestrator for simple/routine tasks

Replaces Claude Code CLI for Reflex-mode tasks, talking to Ollama instead
of the Anthropic API. Executes a tool loop until the model gives a text
response, then outputs JSON matching Claude Code's --output-format json schema.

Usage:
    python3 tam-reflex.py --prompt "check the task queue" \\
        --model gemma4-e4b \\
        --max-turns 30 \\
        --system-prompt-file ~/tam/docs/SOUL-REFLEX.md \\
        --resume <session-id>

# chmod +x tam-reflex.py to make executable
"""

import argparse
import glob as glob_module
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
DB_PATH = TAM_HOME / "data" / "reflex-sessions.db"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

MAX_CONTEXT_TOKENS = 16000   # conservative for 4B models
MAX_TOOL_RESULT_CHARS = 4000
MAX_TURNS_DEFAULT = 30
MAX_GLOB_RESULTS = 100
MAX_REPEAT_TOOL_CALLS = 3    # break loop after this many consecutive identical calls

ALLOWED_WRITE_PATHS = [
    Path.home() / "tam",
    Path.home() / "vaults" / "tam",
]

LOCAL_MODELS: dict[str, str] = {
    "gemma4-e4b": "gemma3:4b-it-qat",   # placeholder until models land in ollama
    "qwen3.5-4b": "qwen3:4b",
    "nemotron-nano": "nemotron-nano:4b",
}

# Dangerous patterns blocked in bash tool
BASH_BLOCKLIST: list[re.Pattern] = [
    re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+/"),   # rm -rf /...
    re.compile(r"rm\s+-[a-z]*f[a-z]*r?\s+/"),
    re.compile(r":\(\)\s*\{"),                    # fork bomb
    re.compile(r"mkfs"),
    re.compile(r"dd\s+.*of=/dev/[sh]d"),
    re.compile(r">\s*/dev/[sh]d"),
    re.compile(r"shutdown\b"),
    re.compile(r"reboot\b"),
    re.compile(r"halt\b"),
    re.compile(r"curl\s.*\|\s*(?:ba)?sh"),   # curl | sh
    re.compile(r"chmod\s+[0-7]*777"),         # world-writable
]

# ── Project intent patterns (simplified from intent-detection.py) ──────────────

PROJECTS = [
    {
        "name": "CVI Aid",
        "patterns": [r"\bcvi[\s\-]aid\b", r"\bcvi\b"],
        "note": "CVI Aid: AR assistive tech for Violet (possible cortical visual impairment). Repo: GeorgeLautenschlager/cvi-aid.",
    },
    {
        "name": "Feudal Carriers",
        "patterns": [r"\bfeu[e]?dal[\s\-]carriers?\b", r"\bfuedal\b"],
        "note": "Feudal Carriers: Java/LWJGL space RTS game. Repo: GeorgeLautenschlager/fuedal-carriers (private).",
    },
    {
        "name": "Project Ender",
        "patterns": [r"\bproject[\s\-]ender\b"],
        "note": "Project Ender: oracle-distilled game AI library. M1 complete, corpus generation phase next. Repo: /home/aldric/Project-Ender/.",
    },
    {
        "name": "Violet / CVI",
        "patterns": [r"\bviolet\b"],
        "note": "Violet is George's infant daughter (twin with Eliana). She has possible cortical visual impairment (CVI). CVI Aid project is built for her.",
    },
]


def detect_project(prompt: str) -> str | None:
    """Return a one-line project note if the prompt mentions a known project."""
    prompt_lower = prompt.lower()
    for project in PROJECTS:
        for pattern in project["patterns"]:
            if re.search(pattern, prompt_lower):
                return project["note"]
    return None


# ── Tool definitions (OpenAI function-calling format) ─────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents with line numbers (cat -n style). Safe for any path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-indexed). Default: 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read. Default: entire file.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Only allowed under ~/tam or ~/vaults/tam.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file.",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file. Fails if old_text is not found exactly once. Only allowed under ~/tam or ~/vaults/tam.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace. Must appear exactly once.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["file_path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return stdout/stderr. Default timeout 120s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default: 120.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files matching a glob pattern. Returns up to 100 results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or '*.md'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Default: current working directory.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using ripgrep (rg). Returns matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex or literal pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search. Default: current working directory.",
                    },
                    "glob_filter": {
                        "type": "string",
                        "description": "Glob filter for file types, e.g. '*.py'. Optional.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def resolve_path(raw: str) -> Path:
    """Expand ~ and return a Path."""
    return Path(os.path.expanduser(raw))


def is_write_allowed(path: Path) -> bool:
    """Return True if path is under one of the allowed write roots."""
    resolved = path.resolve()
    for allowed in ALLOWED_WRITE_PATHS:
        try:
            resolved.relative_to(allowed.resolve())
            return True
        except ValueError:
            continue
    return False


def tool_read_file(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    path = resolve_path(file_path)
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"
    except Exception as e:
        return f"Error reading {path}: {e}"

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else len(lines)
    selected = lines[start:end]

    numbered = "".join(f"{start + i + 1:6}\t{line}" for i, line in enumerate(selected))
    if len(numbered) > MAX_TOOL_RESULT_CHARS:
        numbered = numbered[:MAX_TOOL_RESULT_CHARS] + f"\n... [truncated at {MAX_TOOL_RESULT_CHARS} chars]"
    return numbered


def tool_write_file(file_path: str, content: str) -> str:
    path = resolve_path(file_path)
    if not is_write_allowed(path):
        return (
            f"Error: write not allowed to {path}. "
            f"Allowed paths: {[str(p) for p in ALLOWED_WRITE_PATHS]}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_edit_file(file_path: str, old_text: str, new_text: str) -> str:
    path = resolve_path(file_path)
    if not is_write_allowed(path):
        return (
            f"Error: write not allowed to {path}. "
            f"Allowed paths: {[str(p) for p in ALLOWED_WRITE_PATHS]}"
        )
    try:
        content = path.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as e:
        return f"Error reading {path}: {e}"

    count = content.count(old_text)
    if count == 0:
        return f"Error: old_text not found in {path}"
    if count > 1:
        return f"Error: old_text found {count} times in {path} — must match exactly once"

    new_content = content.replace(old_text, new_text, 1)
    try:
        path.write_text(new_content)
        return f"Edited {path}: replaced {len(old_text)}-char string"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_bash(command: str, timeout: int | None = None) -> str:
    timeout = timeout or 120
    # Safety check
    for pattern in BASH_BLOCKLIST:
        if pattern.search(command):
            return f"Error: command blocked by safety policy: {command[:80]}"
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        if len(output) > MAX_TOOL_RESULT_CHARS:
            output = output[:MAX_TOOL_RESULT_CHARS] + f"\n... [truncated at {MAX_TOOL_RESULT_CHARS} chars]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error running command: {e}"


def tool_glob_files(pattern: str, path: str | None = None) -> str:
    base = resolve_path(path) if path else Path.cwd()
    try:
        matched = list(base.glob(pattern))
    except Exception:
        # Fall back to glob.glob for patterns that pathlib.glob chokes on
        try:
            search = str(base / pattern)
            matched = [Path(p) for p in glob_module.glob(search, recursive=True)]
        except Exception as e:
            return f"Error: {e}"

    matched = sorted(matched, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if len(matched) > MAX_GLOB_RESULTS:
        matched = matched[:MAX_GLOB_RESULTS]
        suffix = f"\n... (limited to {MAX_GLOB_RESULTS} results)"
    else:
        suffix = ""

    if not matched:
        return "No files matched."
    lines = "\n".join(str(p) for p in matched)
    return lines + suffix


def tool_grep(pattern: str, path: str | None = None, glob_filter: str | None = None) -> str:
    cmd = ["rg", "--no-heading", "-n", pattern]
    if glob_filter:
        cmd += ["--glob", glob_filter]
    if path:
        cmd.append(os.path.expanduser(path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout
        if result.stderr and not output:
            output = result.stderr
        if len(output) > MAX_TOOL_RESULT_CHARS:
            output = output[:MAX_TOOL_RESULT_CHARS] + f"\n... [truncated at {MAX_TOOL_RESULT_CHARS} chars]"
        return output or "No matches found."
    except FileNotFoundError:
        return "Error: rg (ripgrep) not found. Install ripgrep."
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30s"
    except Exception as e:
        return f"Error: {e}"


def dispatch_tool(name: str, args: dict) -> str:
    """Route a tool call to the appropriate executor. Returns string result."""
    try:
        if name == "read_file":
            return tool_read_file(
                args["file_path"],
                args.get("offset"),
                args.get("limit"),
            )
        elif name == "write_file":
            return tool_write_file(args["file_path"], args["content"])
        elif name == "edit_file":
            return tool_edit_file(args["file_path"], args["old_text"], args["new_text"])
        elif name == "bash":
            return tool_bash(args["command"], args.get("timeout"))
        elif name == "glob_files":
            return tool_glob_files(args["pattern"], args.get("path"))
        elif name == "grep":
            return tool_grep(args["pattern"], args.get("path"), args.get("glob_filter"))
        else:
            return f"Error: unknown tool '{name}'"
    except KeyError as e:
        return f"Error: missing required argument {e} for tool '{name}'"
    except Exception as e:
        return f"Error executing tool '{name}': {e}"


# ── SQLite session persistence ─────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            model TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            name TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def save_message(conn: sqlite3.Connection, session_id: str, msg: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO messages (session_id, role, content, tool_calls, name, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            msg.get("role", ""),
            msg.get("content"),
            json.dumps(msg["tool_calls"]) if "tool_calls" in msg else None,
            msg.get("name"),
            now,
        ),
    )
    conn.commit()


def load_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, tool_calls, name FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    messages = []
    for row in rows:
        msg: dict = {"role": row["role"]}
        if row["content"] is not None:
            msg["content"] = row["content"]
        if row["tool_calls"] is not None:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            except json.JSONDecodeError:
                pass
        if row["name"] is not None:
            msg["name"] = row["name"]
        messages.append(msg)
    return messages


def create_session(conn: sqlite3.Connection, session_id: str, model: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, model) VALUES (?, ?, ?)",
        (session_id, now, model),
    )
    conn.commit()


def session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row is not None


# ── Context management ─────────────────────────────────────────────────────────

def estimate_tokens(messages: list[dict]) -> int:
    """Rough estimate: 1 token ≈ 4 chars."""
    total = sum(len(json.dumps(m)) for m in messages)
    return total // 4


def trim_context(messages: list[dict]) -> list[dict]:
    """Drop oldest tool-call/tool-result pairs from the middle, keeping:
    - system message (index 0 if present)
    - first user message (always)
    - last 4 exchanges (8 messages)
    """
    if estimate_tokens(messages) <= MAX_CONTEXT_TOKENS:
        return messages

    # Separate system + first user from the rest
    head: list[dict] = []
    tail_start = 0
    for i, m in enumerate(messages):
        if m["role"] in ("system", "user") and i < 2:
            head.append(m)
            tail_start = i + 1
        else:
            break

    body = messages[tail_start:]
    # Keep last 8 messages (4 exchanges) from the body
    preserved_tail = body[-8:] if len(body) > 8 else body

    trimmed = head + preserved_tail
    return trimmed


# ── Ollama API ─────────────────────────────────────────────────────────────────

def ollama_chat(model: str, messages: list[dict], tools: list[dict]) -> dict:
    """Call /api/chat. Returns parsed response dict. Raises on network error."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


# ── System prompt preparation ─────────────────────────────────────────────────

def load_system_prompt(path: str | None) -> str:
    if not path:
        return "You are Tam, a helpful AI assistant."
    expanded = os.path.expanduser(path)
    try:
        with open(expanded) as f:
            return f.read()
    except FileNotFoundError:
        return f"(System prompt file not found: {expanded})\nYou are Tam, a helpful AI assistant."
    except Exception as e:
        return f"(Error loading system prompt: {e})\nYou are Tam, a helpful AI assistant."


def build_system_prompt(base: str, user_prompt: str) -> str:
    """Append datetime and optional project context note."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    system = base.rstrip() + f"\n\nCurrent time: {now_str}\nWorking directory: {Path.cwd()}"

    project_note = detect_project(user_prompt)
    if project_note:
        system += f"\n\n[Context: {project_note}]"

    return system


# ── Main agentic loop ─────────────────────────────────────────────────────────

def resolve_model(name: str) -> str:
    """Map friendly name to ollama model string, or pass through."""
    return LOCAL_MODELS.get(name, name)


def run_loop(
    prompt: str,
    model_key: str,
    max_turns: int,
    system_prompt_raw: str,
    resume_session_id: str | None,
    conn: sqlite3.Connection,
) -> dict:
    """Core tool loop. Returns output dict matching Claude Code JSON schema."""

    ollama_model = resolve_model(model_key)
    system_prompt = build_system_prompt(system_prompt_raw, prompt)

    # Session setup
    if resume_session_id:
        if not session_exists(conn, resume_session_id):
            return {
                "result": f"Error: session '{resume_session_id}' not found in database.",
                "session_id": resume_session_id,
                "total_cost_usd": 0.0,
                "num_turns": 0,
                "stop_reason": "error",
            }
        session_id = resume_session_id
        history = load_messages(conn, session_id)
        # Prepend current system prompt (not persisted, rebuilt each run)
        messages: list[dict] = [{"role": "system", "content": system_prompt}] + history
    else:
        session_id = str(uuid.uuid4())
        create_session(conn, session_id, ollama_model)
        messages = [{"role": "system", "content": system_prompt}]

    # Append new user message
    user_msg: dict = {"role": "user", "content": prompt}
    messages.append(user_msg)
    save_message(conn, session_id, user_msg)

    num_turns = 0
    stop_reason = "end_turn"
    final_text = ""
    last_tool_sig: str | None = None
    repeat_count = 0

    while num_turns < max_turns:
        # Trim context if needed
        messages = trim_context(messages)

        # Call Ollama
        try:
            response = ollama_chat(ollama_model, messages, TOOL_DEFINITIONS)
        except urllib.error.URLError as e:
            return {
                "result": f"Error: could not reach Ollama at {OLLAMA_URL}. Is it running? ({e})",
                "session_id": session_id,
                "total_cost_usd": 0.0,
                "num_turns": num_turns,
                "stop_reason": "error",
            }
        except Exception as e:
            return {
                "result": f"Error calling Ollama: {e}",
                "session_id": session_id,
                "total_cost_usd": 0.0,
                "num_turns": num_turns,
                "stop_reason": "error",
            }

        num_turns += 1
        msg = response.get("message", {})
        assistant_role = msg.get("role", "assistant")
        content_text: str | None = msg.get("content") or None
        tool_calls = msg.get("tool_calls") or []

        # Build assistant message to append
        assistant_msg: dict = {"role": assistant_role}
        if content_text:
            assistant_msg["content"] = content_text
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        if not content_text and not tool_calls:
            assistant_msg["content"] = ""

        messages.append(assistant_msg)
        save_message(conn, session_id, assistant_msg)

        # If no tool calls, we have the final response
        if not tool_calls:
            final_text = content_text or ""
            stop_reason = "end_turn"
            break

        # Execute all tool calls in this response
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args = fn.get("arguments", {})

            # arguments may be a dict or a JSON string
            if isinstance(raw_args, str):
                try:
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    tool_args = {}
            elif isinstance(raw_args, dict):
                tool_args = raw_args
            else:
                tool_args = {}

            # Detect repeated identical tool calls
            tool_sig = json.dumps({"name": tool_name, "args": tool_args}, sort_keys=True)
            if tool_sig == last_tool_sig:
                repeat_count += 1
            else:
                repeat_count = 1
                last_tool_sig = tool_sig

            if repeat_count >= MAX_REPEAT_TOOL_CALLS:
                # Inject a steering message and stop tool execution
                nudge: dict = {
                    "role": "system",
                    "content": (
                        "You have called the same tool with the same arguments multiple times. "
                        "Please stop using tools and provide your final text response now."
                    ),
                }
                messages.append(nudge)
                save_message(conn, session_id, nudge)
                break

            result_text = dispatch_tool(tool_name, tool_args)

            tool_result_msg: dict = {
                "role": "tool",
                "content": result_text,
                "name": tool_name,
            }
            messages.append(tool_result_msg)
            save_message(conn, session_id, tool_result_msg)

    else:
        # Loop exhausted
        stop_reason = "max_turns"
        final_text = content_text or "(max turns reached without final response)"

    return {
        "result": final_text,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "num_turns": num_turns,
        "stop_reason": stop_reason,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="tam-reflex — local-model orchestrator (Ollama backend)",
    )
    parser.add_argument("--prompt", required=True, help="User prompt to send")
    parser.add_argument(
        "--model",
        default="gemma4-e4b",
        help=f"Model name or alias. Known aliases: {list(LOCAL_MODELS.keys())}",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=MAX_TURNS_DEFAULT,
        help=f"Maximum tool-call turns (default: {MAX_TURNS_DEFAULT})",
    )
    default_soul = str(TAM_HOME / "docs" / "SOUL-REFLEX.md")
    parser.add_argument(
        "--system-prompt-file",
        default=default_soul,
        help=f"Path to a file containing the system prompt (default: {default_soul})",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="Resume an existing session by UUID",
    )
    parser.add_argument(
        "--output-format",
        default="json",
        choices=["json"],
        help="Output format (only 'json' supported)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    conn = open_db()

    system_prompt_raw = load_system_prompt(args.system_prompt_file)

    output = run_loop(
        prompt=args.prompt,
        model_key=args.model,
        max_turns=args.max_turns,
        system_prompt_raw=system_prompt_raw,
        resume_session_id=args.resume,
        conn=conn,
    )

    conn.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
