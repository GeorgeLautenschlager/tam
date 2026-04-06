"""One-shot script to set Tam's Discord bot avatar and server icon."""

import asyncio
import sys
from pathlib import Path

import discord

TAM_HOME = Path(__file__).parent.parent  # scripts/ -> tam/
ICON_DIR = TAM_HOME / "icon"

# Load token
DISCORD_TOKEN = ""
env_file = TAM_HOME / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line.startswith("DISCORD_TOKEN="):
            DISCORD_TOKEN = line.split("=", 1)[1].strip().strip("\"'")

if not DISCORD_TOKEN:
    print("Error: DISCORD_TOKEN not found in .env")
    sys.exit(1)

# Use the circle version (Discord clips to circle anyway)
BOT_AVATAR = ICON_DIR / "tam-icon-v5.png"
SERVER_ICON = ICON_DIR / "tam-icon-v5.png"


async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}")

        # Set bot avatar
        avatar_bytes = BOT_AVATAR.read_bytes()
        try:
            await client.user.edit(avatar=avatar_bytes)
            print(f"Bot avatar set from {BOT_AVATAR.name}")
        except Exception as e:
            print(f"Failed to set bot avatar: {e}")

        # Set server icon for all guilds the bot is in
        for guild in client.guilds:
            icon_bytes = SERVER_ICON.read_bytes()
            try:
                await guild.edit(icon=icon_bytes, reason="Tam constellation icon")
                print(f"Server icon set for guild: {guild.name}")
            except discord.Forbidden:
                print(f"No permission to set icon for guild: {guild.name}")
            except Exception as e:
                print(f"Failed to set icon for {guild.name}: {e}")

        print("Done.")
        await client.close()

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
