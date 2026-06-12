"""Shared runtime, config, and mttvalues.com helpers for the MTTV bot.

Feature modules register commands/events against the shared ``bot`` object.
Keep this file free of slash command definitions so value and vote features can be
enabled or removed independently.
"""

import asyncio
import builtins
import io
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from dotenv import load_dotenv


def print_flush(*args: object, **kwargs: object) -> None:
    kwargs.setdefault("flush", True)
    builtins.print(*args, **kwargs)


print = print_flush
load_dotenv()


class StaleAutocompleteFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "discord.app_commands.tree":
            return True
        message = record.getMessage()
        if "Ignoring exception in autocomplete" in message:
            return False
        return True


logging.getLogger("discord.app_commands.tree").addFilter(StaleAutocompleteFilter())

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
ENV_CHANNEL_ID = os.getenv("VOTE_CHANNEL_ID", "").strip()
ENV_INTERVAL_MINUTES = os.getenv("VOTE_INTERVAL_MINUTES", "").strip()
ENV_INTERVAL_SECONDS = os.getenv("VOTE_INTERVAL_SECONDS", "").strip()
ENV_VOTE_TEXT = os.getenv("VOTE_TEXT", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data")))
CONFIG_FILE = DATA_DIR / "vote_config.json"
MTTVALUES_REFRESH_SECONDS = max(60, int(os.getenv("MTTVALUES_REFRESH_SECONDS", "1800")))
VALUE_IMAGE_ATTACHMENT = "mttv-value-image.png"
VALUE_IMAGE_SIZE = (760, 430)
VOTE_TEXT = ENV_VOTE_TEXT or "vote"
APPROVE_REACTION = "\u2b06\ufe0f"
DENY_REACTION = "\u2b07\ufe0f"
NEUTRAL_REACTION = "\u2194\ufe0f"
APPROVE_CHOICE = "approve"
DENY_CHOICE = "deny"
NEUTRAL_CHOICE = "neutral"
VOTE_CHOICES = (APPROVE_CHOICE, NEUTRAL_CHOICE, DENY_CHOICE)
VOTE_REACTIONS = {
    APPROVE_REACTION: APPROVE_CHOICE,
    NEUTRAL_REACTION: NEUTRAL_CHOICE,
    DENY_REACTION: DENY_CHOICE,
}
REACTION_BY_CHOICE = {choice: emoji for emoji, choice in VOTE_REACTIONS.items()}
RARITY_RANDOM = "random"
LIMITED_RARITY = "Limited"
EXOTIC_RARITY = "Exotic"
LEGENDARY_RARITY = "Legendary"
RARITY_FILTERS = (RARITY_RANDOM, LIMITED_RARITY, EXOTIC_RARITY, LEGENDARY_RARITY)
RARITY_MATCH_KEYS = {
    LIMITED_RARITY: {"limited"},
    EXOTIC_RARITY: {"exotic"},
    LEGENDARY_RARITY: {"legendary"},
}
RARITY_STYLE_MAP = {
    "Common": discord.ButtonStyle.secondary,
    "Rare": discord.ButtonStyle.primary,
    "Epic": discord.ButtonStyle.primary,
    "Legendary": discord.ButtonStyle.success,
    "Exotic": discord.ButtonStyle.success,
    "Limited": discord.ButtonStyle.danger,
}
TAG_STYLE_MAP = {
    "rising": discord.ButtonStyle.success,
    "stable": discord.ButtonStyle.primary,
    "dropping": discord.ButtonStyle.danger,
    "underpaid": discord.ButtonStyle.success,
    "overpaid": discord.ButtonStyle.success,
    "meta": discord.ButtonStyle.primary,
    "unstable": discord.ButtonStyle.danger,
}
RARITY_EMOJI_MAP = {
    "Common": "\u26aa",
    "Rare": "\U0001f535",
    "Epic": "\U0001f7e3",
    "Legendary": "\U0001f7e1",
    "Exotic": "\U0001f7e0",
    "Limited": "\U0001f534",
}
TAG_EMOJI_MAP = {
    "rising": "\U0001f7e2",
    "stable": "\U0001f535",
    "dropping": "\U0001f534",
    "underpaid": "\U0001f7e2",
    "overpaid": "\U0001f7e0",
    "meta": "\U0001f7e3",
    "unstable": "\U0001f534",
}
GRAY_COLOR = 0x808080
GREEN_COLOR = 0x2ECC71
RED_COLOR = 0xE74C3C
VALUE_EMBED_COLOR = 0xFF4438
RARITY_EMBED_COLORS = {
    "common": 0x95A5A6,
    "rare": 0x3498DB,
    "epic": 0x9B59B6,
    "legendary": 0xF1C40F,
    "exotic": 0xFF3EB5,
    "limited": 0xE74C3C,
}
MAX_TRACKED_VOTES = 200
AUTOCOMPLETE_CHOICE_LIMIT = 25
AUTOCOMPLETE_CACHE_SECONDS = 600
MTTVALUES_ITEMS_URL = "https://firestore.googleapis.com/v1/projects/military-tycoon-trading-values/databases/(default)/documents/items"
MTTVALUES_FIELD_MASKS = (
    "name",
    "image",
    "value",
    "valueMin",
    "valueMax",
    "value_min",
    "value_max",
    "demand",
    "functionality",
    "tags",
    "rarity",
    "updatedAt",
    "updated_at",
    "createdAt",
    "created_at",
)
MTTVALUES_AUTOCOMPLETE_NAMES: list[str] = []
MTTVALUES_AUTOCOMPLETE_LOADED_AT = 0.0
MTTVALUES_AUTOCOMPLETE_REFRESH_TASK: asyncio.Task | None = None
MTTVALUES_ITEMS_CACHE: list[dict] = []
MTTVALUES_ITEMS_LOADED_AT = 0.0
MTTVALUES_ITEMS_CACHE_SECONDS = MTTVALUES_REFRESH_SECONDS


class VoteBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.reactions = True
        super().__init__(intents=intents, activity=discord.Game(name="/value | mttvalues.com"))
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

        self.loop.create_task(refresh_mttvalues_periodically())
        for task_factory in STARTUP_TASKS:
            self.loop.create_task(task_factory())


STARTUP_TASKS: list = []

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

    message_ids.append(str(message_id))
    guild_config["vote_message_ids"] = message_ids[-MAX_TRACKED_VOTES:]
    tracked_ids = {str(saved_id) for saved_id in guild_config["vote_message_ids"]}
    reaction_votes = guild_config.get("reaction_votes", {})

    if isinstance(reaction_votes, dict):
        kept_votes = {
            str(saved_id): votes
            for saved_id, votes in reaction_votes.items()
            if str(saved_id) in tracked_ids
        }
        kept_votes.setdefault(str(message_id), {})
        guild_config["reaction_votes"] = kept_votes
    else:
        guild_config["reaction_votes"] = {str(message_id): {}}


def record_vote_message(guild_config: dict, channel_id: int, message_id: int) -> None:
    store_vote_message_id(guild_config, message_id)
    guild_config["active_vote_message_id"] = str(message_id)
    guild_config["active_vote_channel_id"] = str(channel_id)


def is_tracked_vote_message(guild_config: dict, message_id: int) -> bool:
    message_key = str(message_id)
    active_message_id = guild_config.get("active_vote_message_id")

    if active_message_id and message_key == str(active_message_id):
        return True

    message_ids = guild_config.get("vote_message_ids", [])
    if not isinstance(message_ids, list):
        return False

    return message_key in {str(saved_id) for saved_id in message_ids}


def format_interval(seconds: int) -> str:
    if seconds % 60 == 0:
        amount = seconds // 60
        unit = "minute" if amount == 1 else "minutes"
    else:
        amount = seconds
        unit = "second" if amount == 1 else "seconds"

    return f"{amount} {unit}"


def bot_can_vote_in(channel: discord.TextChannel, guild: discord.Guild) -> tuple[bool, str]:
    permissions = channel.permissions_for(guild.me)

    if not permissions.send_messages:
        return False, f"I cannot send messages in {channel.mention}."

    if not permissions.embed_links:
        return False, f"I cannot send embeds in {channel.mention}."

    if not permissions.add_reactions:
        return False, f"I cannot add reactions in {channel.mention}."

    if not permissions.read_message_history:
        return False, f"I cannot read message history in {channel.mention}."

    if not permissions.manage_messages:
        return False, f"I need Manage Messages in {channel.mention} to keep reaction votes clean."

    return True, ""


def user_can_manage_guild(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False

    if interaction.user.id == interaction.guild.owner_id:
        return True

    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    if user_can_manage_guild(interaction):
        return True

    await interaction.response.send_message("You need Manage Server permission to use this.", ephemeral=True)
    return False


def empty_vote_counts() -> dict[str, int]:
    return {choice: 0 for choice in VOTE_CHOICES}


def count_votes(votes: dict) -> dict[str, int]:
    counts = empty_vote_counts()

    for choice in votes.values():
        if choice in counts:
            counts[choice] += 1

    return counts


def get_message_reaction_votes(guild_config: dict, message_id: int) -> dict:
    reaction_votes = guild_config.get("reaction_votes", {})

    if not isinstance(reaction_votes, dict):
        return {}

    message_votes = reaction_votes.get(str(message_id), {})
    return message_votes if isinstance(message_votes, dict) else {}


def get_stored_vote_counts(guild_id: int, message_id: int) -> dict[str, int]:
    guild_config = get_guild_config(guild_id)
    return count_votes(get_message_reaction_votes(guild_config, message_id))


def total_votes(counts: dict[str, int]) -> int:
    return sum(counts.get(choice, 0) for choice in VOTE_CHOICES)


def apply_vote_count_footer(embed: discord.Embed, counts: dict[str, int], rarity_text: str | None = None) -> None:
    embed.set_footer(text="mttvalues.com • React to vote")

    if embed.timestamp is None:
        embed.timestamp = datetime.now(timezone.utc)


def set_reaction_vote(guild_id: int, message_id: int, user_id: int, choice: str) -> dict[str, int]:
    config = load_config()
    guild_key = str(guild_id)
    message_key = str(message_id)
    guild_config = config.get(guild_key, {})
    reaction_votes = guild_config.get("reaction_votes", {})

    if not isinstance(reaction_votes, dict):
        reaction_votes = {}

    message_votes = reaction_votes.get(message_key, {})
    if not isinstance(message_votes, dict):
        message_votes = {}

    message_votes[str(user_id)] = choice
    reaction_votes[message_key] = message_votes
    guild_config["reaction_votes"] = reaction_votes
    config[guild_key] = guild_config
    save_config(config)
    return count_votes(message_votes)


def remove_reaction_vote(guild_id: int, message_id: int, user_id: int, choice: str) -> dict[str, int]:
    config = load_config()
    guild_key = str(guild_id)
    message_key = str(message_id)
    user_key = str(user_id)
    guild_config = config.get(guild_key, {})
    reaction_votes = guild_config.get("reaction_votes", {})

    if not isinstance(reaction_votes, dict):
        return empty_vote_counts()

    message_votes = reaction_votes.get(message_key, {})
    if not isinstance(message_votes, dict):
        return empty_vote_counts()

    if message_votes.get(user_key) == choice:
        message_votes.pop(user_key, None)

    reaction_votes[message_key] = message_votes
    guild_config["reaction_votes"] = reaction_votes
    config[guild_key] = guild_config
    save_config(config)
    return count_votes(message_votes)


def get_stored_user_vote(guild_id: int, message_id: int, user_id: int) -> str | None:
    guild_config = get_guild_config(guild_id)
    message_votes = get_message_reaction_votes(guild_config, message_id)
    choice = message_votes.get(str(user_id))
    return choice if choice in VOTE_CHOICES else None


def parse_firestore_value(value: dict) -> object:
    if "stringValue" in value:
        return value["stringValue"]

    if "integerValue" in value:
        return int(value["integerValue"])

    if "doubleValue" in value:
        return float(value["doubleValue"])

    if "booleanValue" in value:
        return bool(value["booleanValue"])

    if "nullValue" in value:
        return None

    if "timestampValue" in value:
        return value["timestampValue"]

    if "arrayValue" in value:
        return [parse_firestore_value(item) for item in value.get("arrayValue", {}).get("values", [])]

    if "mapValue" in value:
        return parse_firestore_fields(value.get("mapValue", {}).get("fields", {}))

    return None


def parse_firestore_fields(fields: dict) -> dict:
    return {key: parse_firestore_value(value) for key, value in fields.items()}


def first_present(*values: object) -> object:
    for value in values:
        if value is not None:
            return value

    return None


def parse_int(value: object) -> int | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []

    return [str(value).strip() for value in values if str(value).strip()]


def normalize_rarity_key(value: object) -> str:
    return str(value).lower().replace(" ", "").replace("_", "").replace("-", "")


def normalize_vehicle_lookup(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def truncate_choice_text(value: object) -> str:
    text = str(value).strip()
    return text[:100]


def canonical_rarity_name(value: object) -> str:
    key = normalize_rarity_key(value)

    for rarity, keys in RARITY_MATCH_KEYS.items():
        if key in keys:
            return rarity

    return str(value).strip()


def item_rarity_values(item: dict) -> list[str]:
    values = normalize_string_list(item.get("rarity"))
    seen = set()
    result = []

    for value in values:
        name = canonical_rarity_name(value)
        if name and name not in seen:
            seen.add(name)
            result.append(name)

    return result


def format_item_rarity(item: dict) -> str:
    values = item_rarity_values(item)
    return ", ".join(values) if values else "Unknown"


def item_rarity_embed_color(item: dict, default_color: int = VALUE_EMBED_COLOR) -> int:
    for rarity in item_rarity_values(item):
        color = RARITY_EMBED_COLORS.get(normalize_rarity_key(rarity))
        if color is not None:
            return color

    return default_color


def item_matches_rarity(item: dict, rarity_filter: str) -> bool:
    if rarity_filter == RARITY_RANDOM:
        return True

    wanted_keys = RARITY_MATCH_KEYS.get(rarity_filter, {normalize_rarity_key(rarity_filter)})
    return any(normalize_rarity_key(value) in wanted_keys for value in item_rarity_values(item))


def find_mttvalues_item(items: list[dict], vehicle_name: str) -> dict | None:
    query = normalize_vehicle_lookup(vehicle_name)
    if not query:
        return None

    exact_matches = []
    prefix_matches = []
    contains_matches = []

    for item in items:
        name_key = normalize_vehicle_lookup(item.get("name", ""))
        if not name_key:
            continue

        if name_key == query:
            exact_matches.append(item)
        elif name_key.startswith(query):
            prefix_matches.append(item)
        elif query in name_key:
            contains_matches.append(item)

    for matches in (exact_matches, prefix_matches, contains_matches):
        if matches:
            return matches[0]

    return None


def unique_vehicle_names(items: list[dict]) -> list[str]:
    names = []
    seen = set()

    for item in sorted(items, key=lambda entry: str(entry.get("name", "")).casefold()):
        name = str(item.get("name", "")).strip()
        key = normalize_vehicle_lookup(name)

        if not name or key in seen:
            continue

        seen.add(key)
        names.append(name)

    return names


def match_vehicle_names(names: list[str], current: str) -> list[str]:
    query = normalize_vehicle_lookup(current)
    if not query:
        return names[:AUTOCOMPLETE_CHOICE_LIMIT]

    prefix_matches = []
    contains_matches = []

    for name in names:
        name_key = normalize_vehicle_lookup(name)
        if name_key.startswith(query):
            prefix_matches.append(name)
        elif query in name_key:
            contains_matches.append(name)

    return (prefix_matches + contains_matches)[:AUTOCOMPLETE_CHOICE_LIMIT]


def normalize_mttvalues_item(raw_item: dict) -> dict:
    value_min = first_present(raw_item.get("valueMin"), raw_item.get("value_min"), raw_item.get("value"))
    value_max = first_present(raw_item.get("valueMax"), raw_item.get("value_max"), raw_item.get("value"))

    return {
        "id": str(raw_item.get("id", "")).strip(),
        "name": str(raw_item.get("name", "Unknown Item")).strip() or "Unknown Item",
        "description": str(raw_item.get("description", "")).strip(),
        "image": str(raw_item.get("image", "")).strip(),
        "value_min": parse_int(value_min),
        "value_max": parse_int(value_max),
        "demand": parse_int(raw_item.get("demand")),
        "functionality": parse_int(raw_item.get("functionality")),
        "tags": normalize_string_list(raw_item.get("tags")),
        "rarity": normalize_string_list(raw_item.get("rarity")),
        "updated_at": first_present(raw_item.get("updatedAt"), raw_item.get("updated_at"), raw_item.get("updateTime")),
        "created_at": first_present(raw_item.get("createdAt"), raw_item.get("created_at"), raw_item.get("createTime")),
    }


def item_from_firestore_document(document: dict) -> dict:
    raw_item = parse_firestore_fields(document.get("fields", {}))
    raw_item["id"] = document.get("name", "").split("/")[-1]
    raw_item["updateTime"] = document.get("updateTime")
    raw_item["createTime"] = document.get("createTime")
    return normalize_mttvalues_item(raw_item)


def fetch_mttvalues_items_sync() -> list[dict]:
    items = []
    page_token = None

    while True:
        query = [("pageSize", "1000")]
        query.extend(("mask.fieldPaths", field) for field in MTTVALUES_FIELD_MASKS)

        if page_token:
            query.append(("pageToken", page_token))

        url = f"{MTTVALUES_ITEMS_URL}?{urlencode(query)}"
        request = Request(url, headers={"User-Agent": "MTTV Vote Bot"})

        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for document in payload.get("documents", []):
            item = item_from_firestore_document(document)
            if item.get("name") and (item.get("value_min") is not None or item.get("value_max") is not None):
                items.append(item)

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return items


def update_mttvalues_memory_cache(items: list[dict], loaded_at: float | None = None) -> list[dict]:
    global MTTVALUES_AUTOCOMPLETE_LOADED_AT
    global MTTVALUES_AUTOCOMPLETE_NAMES
    global MTTVALUES_ITEMS_CACHE
    global MTTVALUES_ITEMS_LOADED_AT

    loaded_at = loaded_at or time.time()
    MTTVALUES_ITEMS_CACHE = items
    MTTVALUES_ITEMS_LOADED_AT = loaded_at
    MTTVALUES_AUTOCOMPLETE_NAMES = unique_vehicle_names(items)
    MTTVALUES_AUTOCOMPLETE_LOADED_AT = loaded_at
    return items


async def refresh_mttvalues_items_cache() -> list[dict]:
    items = await asyncio.to_thread(fetch_mttvalues_items_sync)
    update_mttvalues_memory_cache(items)
    return items


async def get_mttvalues_items(*, force_refresh: bool = False) -> list[dict]:
    cache_age = time.time() - MTTVALUES_ITEMS_LOADED_AT
    if not force_refresh and MTTVALUES_ITEMS_CACHE and cache_age < MTTVALUES_ITEMS_CACHE_SECONDS:
        return MTTVALUES_ITEMS_CACHE

    try:
        return await refresh_mttvalues_items_cache()
    except Exception:
        if MTTVALUES_ITEMS_CACHE:
            return MTTVALUES_ITEMS_CACHE
        raise


async def refresh_mttvalues_autocomplete_cache() -> list[str]:
    try:
        await refresh_mttvalues_items_cache()
    except Exception as error:
        if not MTTVALUES_ITEMS_CACHE:
            print(f"Could not refresh MTTValues cache: {error}")
        return MTTVALUES_AUTOCOMPLETE_NAMES

    return MTTVALUES_AUTOCOMPLETE_NAMES


def get_cached_mttvalues_autocomplete_names() -> list[str]:
    return MTTVALUES_AUTOCOMPLETE_NAMES


def schedule_mttvalues_cache_refresh() -> None:
    global MTTVALUES_AUTOCOMPLETE_REFRESH_TASK

    if MTTVALUES_AUTOCOMPLETE_REFRESH_TASK is None or MTTVALUES_AUTOCOMPLETE_REFRESH_TASK.done():
        MTTVALUES_AUTOCOMPLETE_REFRESH_TASK = asyncio.create_task(refresh_mttvalues_autocomplete_cache())


async def get_mttvalues_autocomplete_names() -> list[str]:
    cache_age = time.time() - MTTVALUES_AUTOCOMPLETE_LOADED_AT
    if MTTVALUES_AUTOCOMPLETE_NAMES and cache_age < AUTOCOMPLETE_CACHE_SECONDS:
        return MTTVALUES_AUTOCOMPLETE_NAMES

    schedule_mttvalues_cache_refresh()

    return MTTVALUES_AUTOCOMPLETE_NAMES


async def refresh_mttvalues_periodically() -> None:
    try:
        await refresh_mttvalues_autocomplete_cache()
    except Exception as error:
        if not MTTVALUES_ITEMS_CACHE:
            print(f"Initial MTTValues refresh failed: {error}")

    while True:
        await asyncio.sleep(MTTVALUES_REFRESH_SECONDS)
        try:
            await refresh_mttvalues_autocomplete_cache()
        except Exception as error:
            if not MTTVALUES_ITEMS_CACHE:
                print(f"Scheduled MTTValues refresh failed: {error}")


async def get_random_mttvalues_item(rarity_filter: str = RARITY_RANDOM) -> dict | None:
    try:
        items = await get_mttvalues_items()
    except Exception as error:
        print(f"Could not fetch MTTValues items: {error}")
        return None

    if not items:
        return None

    filtered_items = [item for item in items if item_matches_rarity(item, rarity_filter)]
    if filtered_items:
        items = filtered_items
    elif rarity_filter != RARITY_RANDOM:
        print(f"No MTTValues items matched rarity {rarity_filter}. Using all items.")

    return random.choice(items)


def format_number(value: object) -> str:
    number = parse_int(value)
    if number is None:
        return "N/A"

    return f"{number:,}"


def average_value(item: dict) -> int | None:
    value_min = parse_int(item.get("value_min"))
    value_max = parse_int(item.get("value_max"))

    if value_min is not None and value_max is not None:
        return round((value_min + value_max) / 2)

    return first_present(value_min, value_max)


def format_value_range(item: dict) -> str:
    value_min = parse_int(item.get("value_min"))
    value_max = parse_int(item.get("value_max"))

    if value_min is not None and value_max is not None:
        if value_min == value_max:
            return format_number(value_min)

        return f"{format_number(value_min)} - {format_number(value_max)}"

    if value_min is not None:
        return format_number(value_min)

    if value_max is not None:
        return format_number(value_max)

    return "No value listed"


def format_score(value: object) -> str:
    number = parse_int(value)
    if number is None:
        return "N/A"

    return str(number)


def format_score_change(old_value: object, new_value: object | None = None) -> str:
    old_text = format_score(old_value)
    if new_value is None:
        return old_text

    return f"{old_text} → {format_score(new_value)}"


def format_value_number(value: object) -> str:
    number = parse_int(value)
    if number is None:
        return "N/A"

    return f"{number:,}"


def format_value_range_from_numbers(value_min: object, value_max: object | None = None) -> str:
    min_text = format_value_number(value_min)
    max_text = format_value_number(value_max)

    if max_text == "N/A" or min_text == max_text:
        return min_text

    return f"{min_text} - {max_text}"


def format_value_change(
    item: dict,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
) -> str:
    old_text = format_value_range(item)

    if new_value_min is None and new_value_max is None:
        return old_text

    value_min = new_value_min if new_value_min is not None else new_value_max
    value_max = new_value_max if new_value_max is not None else value_min
    return f"{old_text} → {format_value_range_from_numbers(value_min, value_max)}"


def format_vehicle_vote_name(item: dict) -> str:
    name = str(item.get("name", VOTE_TEXT)).strip() or VOTE_TEXT
    parts = name.split()

    if len(parts) < 2:
        return name

    code = parts[-1].strip("()")
    has_digit = any(character.isdigit() for character in code)
    is_short_code = len(code) <= 8 and code.upper() == code

    if code and (has_digit or is_short_code) and not name.endswith(f"({code})"):
        return f"{name} ({code})"

    return name


def create_vote_description(
    item: dict | None = None,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
    submitter: str | None = None,
) -> str:
    value_text = "No value listed" if item is None else format_value_change(item, new_value_min, new_value_max)

    return (
        "💎 **Value**\n"
        f"{value_text}"
    )


def create_vote_options_text() -> str:
    return (
        f"{APPROVE_REACTION} **Increase** — the value should increase\n\n"
        f"{NEUTRAL_REACTION} **Keep** — the value should stay the same\n\n"
        f"{DENY_REACTION} **Decrease** — the value should decrease"
    )


def create_vote_embed(
    color: int | None = None,
    item: dict | None = None,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
    new_demand: int | None = None,
    new_functionality: int | None = None,
    status_tags: str | None = None,
    submitter: str | None = None,
) -> discord.Embed:
    if item:
        embed = discord.Embed(
            title=f"🗳️ Value Vote: {format_vehicle_vote_name(item)}",
            description=create_vote_description(item, new_value_min, new_value_max, submitter),
            color=discord.Color(color if color is not None else GRAY_COLOR),
        )

        embed.add_field(name="📈 Demand", value=format_score_change(item.get("demand"), new_demand), inline=True)
        embed.add_field(
            name="⚙️ Functionality",
            value=format_score_change(item.get("functionality"), new_functionality),
            inline=True,
        )
        embed.add_field(name="🏷️ Status Tags", value=(status_tags or "No changes").strip() or "No changes", inline=False)
        embed.add_field(name="\u200b", value=create_vote_options_text(), inline=False)

        image = item.get("image", "")
        if isinstance(image, str) and image.startswith("http"):
            embed.set_thumbnail(url=image)

        apply_vote_count_footer(embed, empty_vote_counts(), format_item_rarity(item))
        return embed

    embed = discord.Embed(
        title=f"🗳️ Value Vote: {VOTE_TEXT}",
        description=create_vote_description(submitter=submitter),
        color=discord.Color(color if color is not None else GRAY_COLOR),
    )
    embed.add_field(name="📈 Demand", value="**N/A**", inline=True)
    embed.add_field(name="⚙️ Functionality", value="**N/A**", inline=True)
    embed.add_field(name="🏷️ Status Tags", value=(status_tags or "No changes").strip() or "No changes", inline=False)
    embed.add_field(name="\u200b", value=create_vote_options_text(), inline=False)
    apply_vote_count_footer(embed, empty_vote_counts())
    return embed


def create_value_embed(item: dict, *, use_attached_image: bool = False) -> discord.Embed:
    updated_at_text = "Unknown"
    updated_at = item.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            updated_at_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            hour_text = updated_at_dt.strftime("%I").lstrip("0") or "0"
            minute_text = updated_at_dt.strftime("%M")
            am_pm_text = updated_at_dt.strftime("%p")
            updated_at_text = f"{updated_at_dt.month}/{updated_at_dt.day}/{updated_at_dt.year} {hour_text}:{minute_text} {am_pm_text}"
        except ValueError:
            updated_at_text = updated_at

    demand_text = format_score(item.get("demand"))
    functionality_text = format_score(item.get("functionality"))

    embed = discord.Embed(
        title=str(item.get("name", "Unknown Item")),
        url="https://mttvalues.com/",
        description=f"\U0001f48e **Value**\n{format_value_range(item)}",
        color=discord.Color(item_rarity_embed_color(item)),
    )

    embed.add_field(name="\U0001f4c8 Demand", value=f"{demand_text}/10" if demand_text != "N/A" else "N/A", inline=True)
    embed.add_field(
        name="\u2699\ufe0f Functionality",
        value=f"{functionality_text}/10" if functionality_text != "N/A" else "N/A",
        inline=True,
    )
    embed.add_field(name="\u2b50 Rarity", value=format_item_rarity(item), inline=True)
    embed.add_field(name="\U0001f3f7\ufe0f Status Tags", value=format_status_tags(item), inline=False)

    image = item.get("image", "")
    if isinstance(image, str) and image.startswith("http"):
        if use_attached_image:
            embed.set_image(url=f"attachment://{VALUE_IMAGE_ATTACHMENT}")
        else:
            embed.set_thumbnail(url=image)

    embed.set_footer(text=f"Source: mttvalues.com | Last updated: {updated_at_text}")
    return embed


def download_image_bytes(image_url: str) -> bytes:
    request = Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return response.read()


async def create_large_value_image_file(item: dict) -> discord.File | None:
    image_url = item.get("image", "")
    if not isinstance(image_url, str) or not image_url.startswith("http"):
        return None

    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    try:
        raw_image = await asyncio.to_thread(download_image_bytes, image_url)
        with Image.open(io.BytesIO(raw_image)) as source:
            source = ImageOps.exif_transpose(source).convert("RGBA")
            max_width, max_height = VALUE_IMAGE_SIZE
            scale = min(max_width / source.width, max_height / source.height)
            target_size = (
                max(1, int(source.width * scale)),
                max(1, int(source.height * scale)),
            )
            resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            resized = source.resize(target_size, resample_filter)

            canvas = Image.new("RGBA", VALUE_IMAGE_SIZE, (0, 0, 0, 0))
            offset = (
                (max_width - target_size[0]) // 2,
                (max_height - target_size[1]) // 2,
            )
            canvas.alpha_composite(resized, offset)

            output = io.BytesIO()
            canvas.save(output, format="PNG")
            output.seek(0)
            return discord.File(output, filename=VALUE_IMAGE_ATTACHMENT)
    except Exception as error:
        print(f"Could not create large value image: {error}")
        return None


def format_score_change(old_value: object, new_value: object | None = None) -> str:
    old_text = format_score(old_value)
    if new_value is None:
        return old_text

    return f"{old_text} \u2192 {format_score(new_value)}"


def format_value_change(
    item: dict,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
) -> str:
    old_text = format_value_range(item)

    if new_value_min is None and new_value_max is None:
        return old_text

    value_min = new_value_min if new_value_min is not None else new_value_max
    value_max = new_value_max if new_value_max is not None else value_min
    return f"{old_text} \u2192 {format_value_range_from_numbers(value_min, value_max)}"


def create_vote_description(
    item: dict | None = None,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
    submitter: str | None = None,
) -> str:
    value_text = "No value listed" if item is None else format_value_change(item, new_value_min, new_value_max)

    return (
        "\U0001f48e **Value**\n"
        f"{value_text}"
    )


def create_vote_options_text() -> str:
    return (
        f"{APPROVE_REACTION} **Increase** \u2014 the value should increase\n\n"
        f"{NEUTRAL_REACTION} **Keep** \u2014 the value should stay the same\n\n"
        f"{DENY_REACTION} **Decrease** \u2014 the value should decrease"
    )


def create_vote_embed(
    color: int | None = None,
    item: dict | None = None,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
    new_demand: int | None = None,
    new_functionality: int | None = None,
    status_tags: str | None = None,
    submitter: str | None = None,
) -> discord.Embed:
    title_name = format_vehicle_vote_name(item) if item else VOTE_TEXT
    embed = discord.Embed(
        title=f"\U0001f5f3\ufe0f Value Vote: {title_name}",
        description=create_vote_description(item, new_value_min, new_value_max, submitter),
        color=discord.Color(color if color is not None else GRAY_COLOR),
    )

    if item:
        demand_text = format_score_change(item.get("demand"), new_demand)
        functionality_text = format_score_change(item.get("functionality"), new_functionality)
        demand = f"{demand_text}/10" if demand_text != "N/A" and "->" not in demand_text and "\u2192" not in demand_text else demand_text
        functionality = (
            f"{functionality_text}/10"
            if functionality_text != "N/A" and "->" not in functionality_text and "\u2192" not in functionality_text
            else functionality_text
        )
        tags = format_status_tags(item) if not status_tags else status_tags.strip()
        image = item.get("image", "")
    else:
        demand = "**N/A**"
        functionality = "**N/A**"
        tags = "No tags"
        image = ""

    embed.add_field(name="\U0001f4c8 Demand", value=demand, inline=True)
    embed.add_field(name="\u2699\ufe0f Functionality", value=functionality, inline=True)
    embed.add_field(
        name="\U0001f3f7\ufe0f Status Tags",
        value=tags or "No tags",
        inline=False,
    )
    embed.add_field(name="\u200b", value=create_vote_options_text(), inline=False)

    if isinstance(image, str) and image.startswith("http"):
        embed.set_thumbnail(url=image)

    embed.set_footer(text="mttvalues.com \u2022 React to vote")
    return embed


def titleize_badge_label(value: object) -> str:
    text = str(value).strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in text.split())


def format_status_tags(item: dict) -> str:
    tags: list[str] = []
    for tag in normalize_string_list(item.get("tags")):
        label = titleize_badge_label(tag)
        if label:
            tags.append(label)

    return ", ".join(tags) if tags else "No tags"


def create_value_badge_view(item: dict) -> discord.ui.View | None:
    badges: list[tuple[str, str | None, discord.ButtonStyle]] = []

    for rarity in item_rarity_values(item):
        label = canonical_rarity_name(rarity)
        if not label:
            continue

        emoji = RARITY_EMOJI_MAP.get(label)
        style = RARITY_STYLE_MAP.get(label, discord.ButtonStyle.secondary)
        badges.append((label, emoji, style))

    for tag in normalize_string_list(item.get("tags")):
        key = str(tag).strip().lower().replace("_", "-")
        label = titleize_badge_label(tag)
        if not label:
            continue

        emoji = TAG_EMOJI_MAP.get(key)
        style = TAG_STYLE_MAP.get(key, discord.ButtonStyle.secondary)
        badges.append((label, emoji, style))

    if not badges:
        return None

    view = discord.ui.View(timeout=None)
    for label, emoji, style in badges[:25]:
        button_kwargs: dict[str, object] = {
            "label": label[:80],
            "style": style,
            "disabled": True,
        }
        if emoji:
            button_kwargs["emoji"] = emoji

        view.add_item(discord.ui.Button(**button_kwargs))

    return view


def get_vote_color(counts: dict[str, int]) -> int:
    max_votes = max(counts.get(choice, 0) for choice in VOTE_CHOICES)
    if max_votes == 0:
        return GRAY_COLOR

    winners = [choice for choice in VOTE_CHOICES if counts.get(choice, 0) == max_votes]
    if len(winners) != 1:
        return GRAY_COLOR

    if winners[0] == APPROVE_CHOICE:
        return GREEN_COLOR

    if winners[0] == DENY_CHOICE:
        return RED_COLOR

    return GRAY_COLOR


def normalize_vote_emoji(emoji: object) -> str:
    return str(emoji).replace("\ufe0f", "")


def choice_from_emoji(emoji: object) -> str | None:
    normalized = normalize_vote_emoji(emoji)

    for vote_emoji, choice in VOTE_REACTIONS.items():
        if normalize_vote_emoji(vote_emoji) == normalized:
            return choice

    return None


async def fetch_vote_message(channel_id: int, message_id: int) -> discord.Message | None:
    channel = bot.get_channel(channel_id)

    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    if not hasattr(channel, "fetch_message"):
        return None

    return await channel.fetch_message(message_id)


async def update_vote_message_embed(message: discord.Message, counts: dict[str, int]) -> None:
    embed = message.embeds[0].copy() if message.embeds else create_vote_embed()
    embed.color = discord.Color(get_vote_color(counts))
    apply_vote_count_footer(embed, counts)
    await message.edit(embed=embed)


async def remove_user_vote_reaction(message: discord.Message, emoji: str, user_id: int) -> None:
    guild = message.guild
    user = guild.get_member(user_id) if guild else None

    if user is None:
        user = discord.Object(id=user_id)

    await message.remove_reaction(emoji, user)


async def remove_other_vote_reactions(
    message: discord.Message,
    payload: discord.RawReactionActionEvent,
    selected_choice: str,
) -> None:
    for choice, emoji in REACTION_BY_CHOICE.items():
        if choice == selected_choice:
            continue

        try:
            await remove_user_vote_reaction(message, emoji, payload.user_id)
        except discord.DiscordException as error:
            print(f"Could not remove old reaction vote: {error}")


async def send_vote(
    channel: discord.abc.Messageable,
    rarity_filter: str = RARITY_RANDOM,
    item: dict | None = None,
    new_value_min: int | None = None,
    new_value_max: int | None = None,
    new_demand: int | None = None,
    new_functionality: int | None = None,
    status_tags: str | None = None,
    submitter: str | None = None,
) -> discord.Message:
    if item is None:
        item = await get_random_mttvalues_item(rarity_filter)

    message = await channel.send(
        embed=create_vote_embed(
            item=item,
            new_value_min=new_value_min,
            new_value_max=new_value_max,
            new_demand=new_demand,
            new_functionality=new_functionality,
            status_tags=status_tags,
            submitter=submitter,
        ),
    )

    for emoji in VOTE_REACTIONS:
        await message.add_reaction(emoji)

    return message


async def safe_defer_interaction(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        return False
    except discord.InteractionResponded:
        return True


async def send_interaction_result(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    file: discord.File | None = None,
    ephemeral: bool = False,
    deferred: bool = True,
) -> None:
    if not deferred:
        return

    try:
        kwargs = {
            "content": content,
            "embed": embed,
            "ephemeral": ephemeral,
        }
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)
    except discord.NotFound:
        return

