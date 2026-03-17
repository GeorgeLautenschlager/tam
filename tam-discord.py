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
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timedelta

import discord

# ── Config ──────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
SOUL_PATH = TAM_HOME / "SOUL.md"
MAX_TURNS = int(os.environ.get("TAM_MAX_TURNS", "10"))
TIMEOUT_SECONDS = int(os.environ.get("TAM_TIMEOUT", "300"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# Only respond to messages from this user (your Discord user ID)
# Set to None to respond to anyone (not recommended)
ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USER_ID")

# Channel name to listen in (None = all channels)
LISTEN_CHANNEL = os.environ.get("TAM_DISCORD_CHANNEL", "tam")

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

async def ask_tam(message: str) -> str:
    """Invoke Claude Code headless with Tam's identity."""
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
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(TAM_HOME),
            ),
            timeout=TIMEOUT_SECONDS + 10,  # buffer beyond claude's own timeout
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=TIMEOUT_SECONDS,
        )

        if proc.returncode != 0:
            log.error(f"Claude exited with code {proc.returncode}: {stderr.decode()}")
            return "Sorry, I hit an error on that one. Check the logs."

        # Parse JSON response
        result = json.loads(stdout.decode())
        text = result.get("result", "No response generated.")
        cost = result.get("total_cost_usd", "unknown")

        log.info(f"Response generated. Cost: ${cost}")

        # Log usage
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
    ledger = TAM_HOME / "USAGE.log"
    timestamp = datetime.now().isoformat()
    entry = f"{timestamp} | discord | cost=${cost} | tokens={json.dumps(usage)}\n"
    with open(ledger, "a") as f:
        f.write(entry)


# ── Discord bot ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info(f"Tam is online as {client.user}")


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
        response = await ask_tam(message.content)

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
