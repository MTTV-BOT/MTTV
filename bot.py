import asyncio
import builtins
import json
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
LIMITED_RARITY = "Limited"
EXOTIC_RARITY = "Exotic"
LEGENDARY_RARITY = "Legendary"
RARITY_FILTERS = (RARITY_RANDOM, LIMITED_RARITY, EXOTIC_RARITY, LEGENDARY_RARITY)
RARITY_MATCH_KEYS = {
    LIMITED_RARITY: {"limited"},
    EXOTIC_RARITY: {"exotic"},
    LEGENDARY_RARITY: {"legendary"},
}
GRAY_COLOR = 0x808080
GREEN_COLOR = 0x2ECC71
RED_COLOR = 0xE74C3C
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

        self.loop.create_task(refresh_mttvalues_autocomplete_cache())
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
    text = (
        "mttvalues.com • React to vote • "
        f"{UPVOTE} {counts.get(HIGHER_CHOICE, 0)} "
        f"{NEUTRAL_VOTE} {counts.get(STAY_CHOICE, 0)} "
        f"{DOWNVOTE} {counts.get(LOWER_CHOICE, 0)}"
    )
    embed.set_footer(text=text)

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


async def get_mttvalues_items() -> list[dict]:
    return await asyncio.to_thread(fetch_mttvalues_items_sync)


async def refresh_mttvalues_autocomplete_cache() -> list[str]:
    global MTTVALUES_AUTOCOMPLETE_LOADED_AT, MTTVALUES_AUTOCOMPLETE_NAMES

    try:
        items = await get_mttvalues_items()
    except Exception as error:
        print(f"Could not refresh MTTValues autocomplete: {error}")
        return MTTVALUES_AUTOCOMPLETE_NAMES

    MTTVALUES_AUTOCOMPLETE_NAMES = unique_vehicle_names(items)
    MTTVALUES_AUTOCOMPLETE_LOADED_AT = time.time()
    return MTTVALUES_AUTOCOMPLETE_NAMES


async def get_mttvalues_autocomplete_names() -> list[str]:
    cache_age = time.time() - MTTVALUES_AUTOCOMPLETE_LOADED_AT
    if MTTVALUES_AUTOCOMPLETE_NAMES and cache_age < AUTOCOMPLETE_CACHE_SECONDS:
        return MTTVALUES_AUTOCOMPLETE_NAMES

    return await refresh_mttvalues_autocomplete_cache()


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

    return f"{number}/10"


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


def create_vote_description(item: dict | None = None) -> str:
    if item is None:
        value_text = "No value listed"
    else:
        value_text = format_value_range(item)

    return (
        "💎 **Value**\n"
        f"**{value_text}**"
    )


def create_vote_options_text() -> str:
    return (
        f"{UPVOTE} **Increase** — the value should increase\n\n"
        f"{NEUTRAL_VOTE} **Keep** — the value should stay the same\n\n"
        f"{DOWNVOTE} **Decrease** — the value should decrease"
    )


def create_vote_embed(
    color: int | None = None,
    item: dict | None = None,
) -> discord.Embed:
    if item:
        embed = discord.Embed(
            title=f"🗳️ Value Vote: {format_vehicle_vote_name(item)}",
            description=create_vote_description(item),
            color=discord.Color(color if color is not None else GRAY_COLOR),
        )

        embed.add_field(name="📈 Demand", value=f"**{format_score(item.get('demand'))}**", inline=True)
        embed.add_field(name="⚙️ Functionality", value=f"**{format_score(item.get('functionality'))}**", inline=True)
        embed.add_field(name="\u200b", value=create_vote_options_text(), inline=False)

        image = item.get("image", "")
        if isinstance(image, str) and image.startswith("http"):
            embed.set_thumbnail(url=image)

        apply_vote_count_footer(embed, empty_vote_counts(), format_item_rarity(item))
        return embed

    embed = discord.Embed(
        title=f"🗳️ Value Vote: {VOTE_TEXT}",
        description=create_vote_description(),
        color=discord.Color(color if color is not None else GRAY_COLOR),
    )
    embed.add_field(name="📈 Demand", value="**N/A**", inline=True)
    embed.add_field(name="⚙️ Functionality", value="**N/A**", inline=True)
    embed.add_field(name="\u200b", value=create_vote_options_text(), inline=False)
    apply_vote_count_footer(embed, empty_vote_counts())
    return embed


def get_vote_color(counts: dict[str, int]) -> int:
    max_votes = max(counts.get(choice, 0) for choice in VOTE_CHOICES)
    if max_votes == 0:
        return GRAY_COLOR

    winners = [choice for choice in VOTE_CHOICES if counts.get(choice, 0) == max_votes]
    if len(winners) != 1:
        return GRAY_COLOR

    if winners[0] == HIGHER_CHOICE:
        return GREEN_COLOR

    if winners[0] == LOWER_CHOICE:
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
) -> discord.Message:
    if item is None:
        item = await get_random_mttvalues_item(rarity_filter)

    message = await channel.send(embed=create_vote_embed(item=item))

    for emoji in VOTE_REACTIONS:
        await message.add_reaction(emoji)

    return message


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if bot.user and payload.user_id == bot.user.id:
        return

    choice = choice_from_emoji(payload.emoji)
    if choice is None or payload.guild_id is None:
        return

    guild_config = get_guild_config(payload.guild_id)

    if not is_tracked_vote_message(guild_config, payload.message_id):
        return

    try:
        message = await fetch_vote_message(payload.channel_id, payload.message_id)
    except discord.DiscordException as error:
        print(f"Could not fetch vote message: {error}")
        return

    if message is None:
        return

    counts = set_reaction_vote(payload.guild_id, payload.message_id, payload.user_id, choice)
    await remove_other_vote_reactions(message, payload, choice)
    await update_vote_message_embed(message, counts)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if bot.user and payload.user_id == bot.user.id:
        return

    choice = choice_from_emoji(payload.emoji)
    if choice is None or payload.guild_id is None:
        return

    guild_config = get_guild_config(payload.guild_id)

    if not is_tracked_vote_message(guild_config, payload.message_id):
        return

    if get_stored_user_vote(payload.guild_id, payload.message_id, payload.user_id) != choice:
        return

    counts = remove_reaction_vote(payload.guild_id, payload.message_id, payload.user_id, choice)

    try:
        message = await fetch_vote_message(payload.channel_id, payload.message_id)
    except discord.DiscordException as error:
        print(f"Could not fetch vote message: {error}")
        return

    if message is not None:
        await update_vote_message_embed(message, counts)


@bot.tree.command(name="channel", description="Choose the channel for automatic votes.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="The channel where new votes should be posted.")
async def channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    allowed, error = bot_can_vote_in(channel, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    set_guild_config(interaction.guild.id, {"channel_id": channel.id})
    await interaction.response.send_message(f"Vote channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="time", description="Set how often a new vote should be posted.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(amount="The number of minutes to wait between votes.")
async def time_command(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100000],
):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    guild_config = get_guild_config(interaction.guild.id)
    channel_id = guild_config.get("channel_id")
    if not channel_id:
        await interaction.response.send_message("Set a vote channel first with `/channel`.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("The saved vote channel no longer exists. Set it again with `/channel`.", ephemeral=True)
        return

    allowed, error = bot_can_vote_in(channel, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    interval_seconds = amount * 60
    updates = {"interval_seconds": interval_seconds}
    if guild_config.get("enabled"):
        updates["next_vote_at"] = time.time() + interval_seconds

    set_guild_config(interaction.guild.id, updates)

    if guild_config.get("enabled"):
        await interaction.response.send_message(
            f"Vote timer updated. New votes will be posted in {channel.mention} every {format_interval(interval_seconds)}.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Vote interval set to {format_interval(interval_seconds)}. Use `/votestart` to start counting.",
            ephemeral=True,
        )


@bot.tree.command(name="rarity", description="Choose which rarity can appear in votes.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(rarity="The rarity filter for new votes.")
@app_commands.choices(
    rarity=[
        app_commands.Choice(name="Random", value=RARITY_RANDOM),
        app_commands.Choice(name="Limited", value=LIMITED_RARITY),
        app_commands.Choice(name="Exotic", value=EXOTIC_RARITY),
        app_commands.Choice(name="Legendary", value=LEGENDARY_RARITY),
    ]
)
async def rarity_command(
    interaction: discord.Interaction,
    rarity: app_commands.Choice[str],
):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    set_guild_config(interaction.guild.id, {"rarity_filter": rarity.value})

    if rarity.value == RARITY_RANDOM:
        await interaction.response.send_message("Vote rarity set to random.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Vote rarity set to {rarity.value}.", ephemeral=True)


@bot.tree.command(name="voteforce", description="Start a vote for one vehicle now.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(vehicle_name="The vehicle name to use for the vote.")
async def voteforce(interaction: discord.Interaction, vehicle_name: str):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    guild_config = get_guild_config(interaction.guild.id)
    channel_id = guild_config.get("channel_id")
    if not channel_id:
        await interaction.followup.send("Set a vote channel first with `/channel`.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.DiscordException as error:
            print(f"Could not find vote channel for guild {interaction.guild.id}: {error}")
            await interaction.followup.send("The saved vote channel no longer exists. Set it again with `/channel`.", ephemeral=True)
            return

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("The saved vote channel no longer exists. Set it again with `/channel`.", ephemeral=True)
        return

    allowed, error = bot_can_vote_in(channel, interaction.guild)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    try:
        items = await get_mttvalues_items()
    except Exception as error:
        print(f"Could not fetch MTTValues items: {error}")
        await interaction.followup.send("Could not fetch mttvalues.com right now.", ephemeral=True)
        return

    item = find_mttvalues_item(items, vehicle_name)
    if item is None:
        await interaction.followup.send(f"Could not find `{vehicle_name}` on mttvalues.com.", ephemeral=True)
        return

    try:
        message = await send_vote(channel, item=item)
    except discord.DiscordException as error:
        print(f"Could not send forced vote for guild {interaction.guild.id}: {error}")
        await interaction.followup.send("Could not send the forced vote.", ephemeral=True)
        return

    config = load_config()
    guild_key = str(interaction.guild.id)
    guild_config = config.get(guild_key, {})
    record_vote_message(guild_config, channel.id, message.id)
    config[guild_key] = guild_config
    save_config(config)

    await interaction.followup.send(
        f"Forced vote started for {item.get('name', vehicle_name)} in {channel.mention}.",
        ephemeral=True,
    )


@voteforce.autocomplete("vehicle_name")
async def voteforce_vehicle_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    names = await get_mttvalues_autocomplete_names()
    matches = match_vehicle_names(names, current)

    return [
        app_commands.Choice(name=truncate_choice_text(name), value=truncate_choice_text(name))
        for name in matches
    ]


@bot.tree.command(name="votestart", description="Start the automatic vote timer.")
@app_commands.default_permissions(manage_guild=True)
async def votestart(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    guild_config = get_guild_config(interaction.guild.id)
    channel_id = guild_config.get("channel_id")
    if not channel_id:
        await interaction.response.send_message("Set a vote channel first with `/channel`.", ephemeral=True)
        return

    interval_seconds = guild_config.get("interval_seconds")
    if not interval_seconds:
        await interaction.response.send_message("Set a vote interval first with `/time`.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("The saved vote channel no longer exists. Set it again with `/channel`.", ephemeral=True)
        return

    allowed, error = bot_can_vote_in(channel, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    set_guild_config(
        interaction.guild.id,
        {
            "enabled": True,
            "next_vote_at": time.time() + int(interval_seconds),
        },
    )
    await interaction.response.send_message(
        f"Vote timer started. Next vote in {format_interval(int(interval_seconds))}.",
        ephemeral=True,
    )


@bot.tree.command(name="votestop", description="Stop the automatic vote timer.")
@app_commands.default_permissions(manage_guild=True)
async def votestop(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not await require_manage_guild(interaction):
        return

    guild_config = get_guild_config(interaction.guild.id)
    if not guild_config.get("enabled"):
        await interaction.response.send_message("Vote timer is already stopped.", ephemeral=True)
        return

    set_guild_config(interaction.guild.id, {"enabled": False, "next_vote_at": None})
    await interaction.response.send_message("Vote timer stopped.", ephemeral=True)


async def vote_worker():
    await bot.wait_until_ready()
    apply_env_config_defaults()

    while not bot.is_closed():
        config = load_config()
        now = time.time()
        changed = False

        for guild_id, guild_config in config.items():
            channel_id = guild_config.get("channel_id")
            interval_seconds = guild_config.get("interval_seconds")
            next_vote_at = guild_config.get("next_vote_at")
            rarity_filter = guild_config.get("rarity_filter", RARITY_RANDOM)

            if rarity_filter not in RARITY_FILTERS:
                rarity_filter = RARITY_RANDOM

            if not guild_config.get("enabled"):
                continue

            if not channel_id or not interval_seconds:
                continue

            if not next_vote_at:
                guild_config["next_vote_at"] = now + int(interval_seconds)
                changed = True
                continue

            if now < float(next_vote_at):
                continue

            channel = bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except discord.DiscordException as error:
                    print(f"Could not find vote channel for guild {guild_id}: {error}")
                    continue

            try:
                message = await send_vote(channel, rarity_filter)
            except discord.DiscordException as error:
                print(f"Could not send vote for guild {guild_id}: {error}")
                continue

            record_vote_message(guild_config, int(getattr(channel, "id", channel_id)), message.id)

            while float(next_vote_at) <= now:
                next_vote_at = float(next_vote_at) + int(interval_seconds)

            guild_config["next_vote_at"] = next_vote_at
            changed = True
            save_config(config)
            changed = False

        if changed:
            save_config(config)

        await asyncio.sleep(10)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")

    start_health_server()
    bot.run(TOKEN)
