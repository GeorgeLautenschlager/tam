#!/usr/bin/env python3
"""
tam-discord.py — Discord bridge for Tam

Listens for messages in a Discord server, invokes Claude Code in headless mode
with Tam's identity, and posts the response back.

Setup:
    1. Create a Discord app at https://discord.com/developers/applications
    2. Enable MESSAGE CONTENT intent in the Bot settings
    3. Invite to your server with bot + message permissions
    4. Copy the bot token into ~/tam/.env

Usage:
    python3 tam-discord.py
"""

import asyncio
import json
import os
import re
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

# ── Config ──────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
SOUL_PATH = TAM_HOME / "docs" / "SOUL.md"
MAX_TURNS = int(os.environ.get("TAM_MAX_TURNS", "10"))
TIMEOUT_SECONDS = int(os.environ.get("TAM_TIMEOUT", "600"))

_MODEL_SHORTHANDS = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}
_raw_model = os.environ.get("TAM_MODEL", "opus")
MODEL = _MODEL_SHORTHANDS.get(_raw_model, _raw_model)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# Only respond to messages from this user (your Discord user ID)
# Set to None to respond to anyone (not recommended)
ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USER_ID")

# Channel name to listen in (None = all channels)
LISTEN_CHANNEL = os.environ.get("TAM_DISCORD_CHANNEL", "tam")

# How many hours of inactivity before a session is considered stale
SESSION_TIMEOUT_HOURS = int(os.environ.get("TAM_SESSION_TIMEOUT_HOURS", "4"))

# Where channel sessions are persisted
SESSION_FILE = TAM_HOME / "data" / "sessions.json"

# Discord message length limit
MAX_MESSAGE_LENGTH = 2000

# ── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(TAM_HOME / "logs" / "discord.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tam-discord")

# ── Session management ──────────────────────────────────────────────────────

def load_sessions() -> dict:
    """Load persisted channel sessions from disk."""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_sessions(sessions: dict) -> None:
    SESSION_FILE.write_text(json.dumps(sessions, indent=2))


def get_session_id(channel_id: int) -> str | None:
    """Return session_id for a channel if it exists and isn't stale."""
    sessions = load_sessions()
    entry = sessions.get(str(channel_id))
    if not entry:
        return None
    last_used = datetime.fromisoformat(entry["last_used"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SESSION_TIMEOUT_HOURS)
    if last_used < cutoff:
        log.info(f"Session for channel {channel_id} expired — starting fresh")
        return None
    return entry["session_id"]


def save_session_id(channel_id: int, session_id: str) -> None:
    sessions = load_sessions()
    sessions[str(channel_id)] = {
        "session_id": session_id,
        "last_used": datetime.now(timezone.utc).isoformat(),
    }
    save_sessions(sessions)


# ── Budget check ──────────────────────────────────────────────────────────

def check_discord_budget() -> tuple[bool, dict]:
    """Check if Discord has budget remaining.

    Fail-open: if the checker errors, allow the message through.
    George's direct interactions should never be gated by infra bugs.
    """
    try:
        result = subprocess.run(
            ["python3", str(TAM_HOME / "tam-budget.py"), "--source", "discord"],
            capture_output=True, text=True, timeout=5,
            cwd=str(TAM_HOME),
        )
        if result.returncode == 2:  # checker error — fail open
            log.warning(f"Budget checker error: {result.stderr.strip()}")
            return True, {}
        data = json.loads(result.stdout)
        return data.get("allowed", True), data
    except Exception as e:
        log.warning(f"Budget check failed (allowing): {e}")
        return True, {}


# ── Load identity ───────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    """Load SOUL.md as system prompt, refreshed each invocation."""
    soul = SOUL_PATH.read_text() if SOUL_PATH.exists() else ""
    return f"""{soul}

You are in Discord mode. George is messaging you through Discord.
Keep responses concise — Discord has a 2000 character limit per message.
If you need to give a long answer, break it into key points.
Read MEMORY.md and STATE.md if you need context, but don't do it for
simple questions or casual chat — use judgment about when it's worth the tokens.
Update STATE.md or MEMORY.md if George tells you something worth remembering."""


# ── Claude invocation ───────────────────────────────────────────────────────

async def ask_tam(message: str, channel_id: int, allowed_tools: str = "Read,Write") -> str:
    """Invoke Claude Code headless with Tam's identity, resuming session if available."""
    # Budget check — fail-open so George can always talk to Tam
    allowed, budget_data = check_discord_budget()
    if not allowed:
        daily_used = budget_data.get("daily_used", "?")
        daily_limit = budget_data.get("daily_limit", "?")
        reason = budget_data.get("reason", "unknown")
        log.info(f"Discord budget blocked: {reason}")
        return (
            f"I'm over budget for today (${daily_used} / ${daily_limit} daily). "
            f"I'll be back tomorrow, or you can adjust `data/budget.json` to override."
        )

    session_id = get_session_id(channel_id)
    resuming = session_id is not None

    if resuming:
        log.info(f"Resuming session {session_id} for channel {channel_id}")
        cmd = [
            "claude", "-p", message,
            "--model", MODEL,
            "--resume", session_id,
            "--allowedTools", allowed_tools,
            "--dangerously-skip-permissions",
            "--max-turns", str(MAX_TURNS),
            "--output-format", "json",
        ]
    else:
        log.info(f"Starting new session for channel {channel_id}")
        system_prompt = load_system_prompt()
        cmd = [
            "claude", "-p", message,
            "--model", MODEL,
            "--append-system-prompt", system_prompt,
            "--allowedTools", allowed_tools,
            "--dangerously-skip-permissions",
            "--max-turns", str(MAX_TURNS),
            "--output-format", "json",
        ]

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(TAM_HOME),
            ),
            timeout=TIMEOUT_SECONDS + 10,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=TIMEOUT_SECONDS,
        )

        # Parse JSON response (attempt even on non-zero exit — rate limits return valid JSON)
        try:
            result = json.loads(stdout.decode())
        except json.JSONDecodeError:
            if proc.returncode != 0:
                log.error(f"Claude exited with code {proc.returncode}: {stderr.decode()}")
                return "Sorry, I hit an error on that one. Check the logs."
            raise

        text = result.get("result", "No response generated.")
        cost = result.get("total_cost_usd", "unknown")

        # Rate limit detection — match the specific CLI message, not general discussion
        if cost == 0 and re.search(r"You've hit your limit", text):
            log.info(f"Rate limited: {text}")
            return text

        if proc.returncode != 0:
            log.error(f"Claude exited with code {proc.returncode}: {stderr.decode()}")
            return "Sorry, I hit an error on that one. Check the logs."

        # Persist session ID for next message
        new_session_id = result.get("session_id")
        if new_session_id:
            save_session_id(channel_id, new_session_id)
            log.info(f"Session {new_session_id} saved for channel {channel_id}")
        else:
            log.warning("No session_id in response — next message will start fresh")

        log.info(f"Response generated. Cost: ${cost} Model: {MODEL}")
        log_usage(cost, result.get("usage", {}))

        return text

    except asyncio.TimeoutError:
        log.error("Claude Code timed out")
        return "I timed out on that one. Try a simpler question or try again."
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude output: {e}")
        return "Got a garbled response. Check the logs."
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return f"Something went wrong: {e}"


def log_usage(cost: float, usage: dict) -> None:
    """Append usage data to a simple ledger."""
    ledger = TAM_HOME / "data" / "USAGE.log"
    timestamp = datetime.now().isoformat()
    entry = f"{timestamp} | discord | cost=${cost} | tokens={json.dumps(usage)}\n"
    with open(ledger, "a") as f:
        f.write(entry)


# ── Discord bot ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="reflect", description="Run Tam's self-reflection and memory audit")
async def slash_reflect(interaction: discord.Interaction):
    # Auth check
    if ALLOWED_USER_ID and str(interaction.user.id) != ALLOWED_USER_ID:
        await interaction.response.send_message(
            "You are not authorized to use this command.", ephemeral=True
        )
        return

    await interaction.response.defer()
    log.info(f"Slash command /reflect from {interaction.user}")
    reflect_tools = "Read,Glob,Grep,Bash,Write,Edit,Agent"
    response = await ask_tam("/reflect", interaction.channel_id, allowed_tools=reflect_tools)
    chunks = split_message(response)
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


@tree.command(name="budget", description="Show Tam's current budget status")
async def slash_budget(interaction: discord.Interaction):
    if ALLOWED_USER_ID and str(interaction.user.id) != ALLOWED_USER_ID:
        await interaction.response.send_message(
            "You are not authorized to use this command.", ephemeral=True
        )
        return

    try:
        result = subprocess.run(
            ["python3", str(TAM_HOME / "tam-budget.py"), "--summary"],
            capture_output=True, text=True, timeout=5,
            cwd=str(TAM_HOME),
        )
        summary = result.stdout.strip() or "Could not retrieve budget summary."
        await interaction.response.send_message(f"```\n{summary}\n```")
    except Exception as e:
        await interaction.response.send_message(f"Budget check failed: {e}")


@client.event
async def on_ready():
    await tree.sync()
    log.info(f"Tam is online as {client.user} — slash commands synced")


@client.event
async def on_message(message: discord.Message):
    # Don't respond to ourselves
    if message.author == client.user:
        return

    # Filter by user if configured
    if ALLOWED_USER_ID and str(message.author.id) != ALLOWED_USER_ID:
        return

    # Filter by channel name if configured
    if LISTEN_CHANNEL and hasattr(message.channel, "name"):
        if message.channel.name != LISTEN_CHANNEL:
            return

    # Ignore empty messages and embeds-only
    if not message.content.strip():
        return

    log.info(f"Message from {message.author}: {message.content[:100]}...")

    # Show typing indicator while Tam thinks
    async with message.channel.typing():
        response = await ask_tam(message.content, message.channel.id)

    # Split long responses across multiple messages
    chunks = split_message(response)
    for chunk in chunks:
        await message.channel.send(chunk)


def split_message(text: str) -> list[str]:
    """Split a response into Discord-friendly chunks."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        # Try to split at a newline
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            # No newline, split at space
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            # No space either, hard split
            split_at = MAX_MESSAGE_LENGTH

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    return chunks


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        # Try loading from .env file
        env_file = TAM_HOME / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("DISCORD_TOKEN="):
                    DISCORD_TOKEN = line.split("=", 1)[1].strip().strip('"\'')
                if line.startswith("DISCORD_ALLOWED_USER_ID="):
                    ALLOWED_USER_ID = line.split("=", 1)[1].strip().strip('"\'')

    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not set. Add it to ~/tam/.env or environment.")
        print('  echo \'DISCORD_TOKEN=your-token-here\' >> ~/tam/.env')
        exit(1)

    # Ensure log directory exists
    (TAM_HOME / "logs").mkdir(exist_ok=True)

    log.info(f"Starting Tam Discord bridge. TAM_HOME={TAM_HOME}")
    log.info(f"Listening in channel: {LISTEN_CHANNEL or 'all channels'}")
    client.run(DISCORD_TOKEN)
