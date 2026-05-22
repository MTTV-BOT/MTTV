import asyncio
import builtins
import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from dotenv import load_dotenv


def print_flush(*args: object, **kwargs: object) -> None:
    kwargs.setdefault("flush", True)
    builtins.print(*args, **kwargs)


print = print_flush
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
ENV_CHANNEL_ID = os.getenv("VOTE_CHANNEL_ID", "").strip()
ENV_INTERVAL_MINUTES = os.getenv("VOTE_INTERVAL_MINUTES", "").strip()
ENV_INTERVAL_SECONDS = os.getenv("VOTE_INTERVAL_SECONDS", "").strip()
ENV_VOTE_TEXT = os.getenv("VOTE_TEXT", "").strip()

DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "vote_config.json"
VOTE_TEXT = ENV_VOTE_TEXT or "vote"
UPVOTE = "\u2b06\ufe0f"
NEUTRAL_VOTE = "\u2194\ufe0f"
DOWNVOTE = "\u2b07\ufe0f"
HIGHER_CHOICE = "higher"
STAY_CHOICE = "stay"
LOWER_CHOICE = "lower"
VOTE_CHOICES = (HIGHER_CHOICE, STAY_CHOICE, LOWER_CHOICE)
VOTE_REACTIONS = {
    UPVOTE: HIGHER_CHOICE,
    NEUTRAL_VOTE: STAY_CHOICE,
    DOWNVOTE: LOWER_CHOICE,
}
REACTION_BY_CHOICE = {choice: emoji for emoji, choice in VOTE_REACTIONS.items()}
RARITY_RANDOM = "random"
LIMITED_RARITY = "Limitededition"
EXOTIC_RARITY = "Exotic"
LEGENDARY_RARITY = "Legendary"
RARITY_FILTERS = (RARITY_RANDOM, LIMITED_RARITY, EXOTIC_RARITY, LEGENDARY_RARITY)
RARITY_MATCH_KEYS = {
    LIMITED_RARITY: {"limited", "limitededition", "limitededitions"},
    EXOTIC_RARITY: {"exotic"},
    LEGENDARY_RARITY: {"legendary"},
}
GRAY_COLOR = 0x808080
GREEN_COLOR = 0x2ECC71
RED_COLOR = 0xE74C3C
MAX_TRACKED_VOTES = 200
MTTVALUES_ITEMS_URL = "https://firestore.googleapis.com/v1/projects/military-tycoon-trading-values/databases/(default)/documents/items"


class VoteBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        command_names = [command.name for command in self.tree.get_commands()]
        print(f"Registered local command(s): {', '.join(command_names)}")

        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                print(f"Clearing old guild command(s) for GUILD_ID={GUILD_ID}.")
                self.tree.clear_commands(guild=guild)
                guild_commands = await self.tree.sync(guild=guild)
                print(f"Guild command sync now has {len(guild_commands)} command(s).")
            except ValueError:
                print("GUILD_ID must be a number. Skipping guild command cleanup.")
            except discord.DiscordException as error:
                print(f"Guild command cleanup failed: {error}")
        else:
            print("GUILD_ID is empty. Old guild command duplicates cannot be cleared automatically.")

        try:
            global_commands = await self.tree.sync()
            print(f"Synced {len(global_commands)} global command(s).")
        except discord.DiscordException as error:
            print(f"Global command sync failed: {error}")

        self.loop.create_task(vote_worker())


bot = VoteBot()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args: object) -> None:
        return


def start_health_server() -> None:
    def serve() -> None:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        server.serve_forever()

    Thread(target=serve, daemon=True).start()


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def save_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)


def get_guild_config(guild_id: int) -> dict:
    config = load_config()
    return config.get(str(guild_id), {})


def set_guild_config(guild_id: int, updates: dict) -> dict:
    config = load_config()
    guild_key = str(guild_id)
    guild_config = config.get(guild_key, {})
    guild_config.update(updates)
    config[guild_key] = guild_config
    save_config(config)
    return guild_config


def apply_env_config_defaults() -> None:
    if not GUILD_ID or not ENV_CHANNEL_ID or not (ENV_INTERVAL_MINUTES or ENV_INTERVAL_SECONDS):
        return

    try:
        guild_id = int(GUILD_ID)
        channel_id = int(ENV_CHANNEL_ID)
    except ValueError:
        print("GUILD_ID and VOTE_CHANNEL_ID must be numbers.")
        return

    try:
        if ENV_INTERVAL_MINUTES:
            interval_seconds = int(ENV_INTERVAL_MINUTES) * 60
        else:
            interval_seconds = int(ENV_INTERVAL_SECONDS)
    except ValueError:
        print("VOTE_INTERVAL_MINUTES and VOTE_INTERVAL_SECONDS must be numbers.")
        return

    if interval_seconds < 1:
        print("Vote interval must be at least 1.")
        return

    guild_config = get_guild_config(guild_id)
    updates = {}

    if not guild_config.get("channel_id"):
        updates["channel_id"] = channel_id

    if not guild_config.get("interval_seconds"):
        updates["interval_seconds"] = interval_seconds

    if "enabled" not in guild_config:
        updates["enabled"] = False

    if updates:
        set_guild_config(guild_id, updates)


def store_vote_message_id(guild_config: dict, message_id: int) -> None:
    message_ids = guild_config.get("vote_message_ids", [])
    if not isinstance(message_ids, list):
        message_ids = []
