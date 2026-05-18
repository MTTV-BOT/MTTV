import asyncio
import builtins
import json
import os
import random
import time
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
ENV_INTERVAL_SECONDS = os.getenv("VOTE_INTERVAL_SECONDS", "").strip()
ENV_VOTE_TEXT = os.getenv("VOTE_TEXT", "").strip()

DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "vote_config.json"
VOTE_TEXT = ENV_VOTE_TEXT or "vote"
UPVOTE = "\u2b06\ufe0f"
NEUTRAL_VOTE = "\u2194\ufe0f"
DOWNVOTE = "\u2b07\ufe0f"
VOTE_REACTIONS = (UPVOTE, NEUTRAL_VOTE, DOWNVOTE)
GRAY_COLOR = 0x808080
GREEN_COLOR = 0x2ECC71
RED_COLOR = 0xE74C3C
MAX_TRACKED_VOTES = 200
MTTVALUES_ITEMS_URL = "https://firestore.googleapis.com/v1/projects/military-tycoon-trading-values/databases/(default)/documents/items"
ITEM_CACHE_SECONDS = 900
item_cache: dict[str, object] = {"items": [], "updated_at": 0.0}

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
    if not GUILD_ID or not ENV_CHANNEL_ID or not ENV_INTERVAL_SECONDS:
        return

    try:
        guild_id = int(GUILD_ID)
        channel_id = int(ENV_CHANNEL_ID)
        interval_seconds = int(ENV_INTERVAL_SECONDS)
    except ValueError:
        print("GUILD_ID, VOTE_CHANNEL_ID, and VOTE_INTERVAL_SECONDS must be numbers.")
        return

    if interval_seconds < 1:
        print("VOTE_INTERVAL_SECONDS must be at least 1.")
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


def remember_vote_message(guild_id: int, message_id: int) -> None:
    config = load_config()
    guild_key = str(guild_id)
    guild_config = config.get(guild_key, {})
    store_vote_message_id(guild_config, message_id)
    config[guild_key] = guild_config
    save_config(config)


def is_tracked_vote_message(guild_id: int, message_id: int) -> bool:
    guild_config = get_guild_config(guild_id)
    message_ids = guild_config.get("vote_message_ids", [])
    return str(message_id) in {str(saved_id) for saved_id in message_ids}


def interval_to_seconds(amount: int, unit: str) -> int:
    multipliers = {
        "seconds": 1,
        "minutes": 60,
        "hours": 60 * 60,
    }
    return amount * multipliers[unit]


def format_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        amount = seconds // 3600
        unit = "hour" if amount == 1 else "hours"
    elif seconds % 60 == 0:
        amount = seconds // 60
        unit = "minute" if amount == 1 else "minutes"
    else:
        amount = seconds
        unit = "second" if amount == 1 else "seconds"

    return f"{amount} {unit}"


def normalize_emoji(emoji: object) -> str:
    return str(emoji).replace("\ufe0f", "")


def empty_vote_counts() -> dict[str, int]:
    return {
        normalize_emoji(UPVOTE): 0,
        normalize_emoji(NEUTRAL_VOTE): 0,
        normalize_emoji(DOWNVOTE): 0,
    }


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


def format_vote_counts(counts: dict[str, int]) -> str:
    return (
        f"{UPVOTE} {counts[normalize_emoji(UPVOTE)]}   "
        f"{NEUTRAL_VOTE} {counts[normalize_emoji(NEUTRAL_VOTE)]}   "
        f"{DOWNVOTE} {counts[normalize_emoji(DOWNVOTE)]}"
    )


def parse_firestore_value(value: dict) -> object:
    if "stringValue" in value:
        return value["stringValue"]

    if "integerValue" in value:
        return int(value["integerValue"])

    if "doubleValue" in value:
        return float(value["doubleValue"])

    if "booleanValue" in value:
        return bool(value["booleanValue"])

    if "timestampValue" in value:
        return value["timestampValue"]

    if "arrayValue" in value:
        return [parse_firestore_value(item) for item in value.get("arrayValue", {}).get("values", [])]

    if "mapValue" in value:
        return parse_firestore_fields(value.get("mapValue", {}).get("fields", {}))

    return None


def parse_firestore_fields(fields: dict) -> dict:
    return {key: parse_firestore_value(value) for key, value in fields.items()}


def fetch_mttvalues_items_sync() -> list[dict]:
    items = []
    page_token = None

    while True:
        query = {"pageSize": "1000"}
        if page_token:
            query["pageToken"] = page_token

        url = f"{MTTVALUES_ITEMS_URL}?{urlencode(query)}"
        request = Request(url, headers={"User-Agent": "MTTV Vote Bot"})

        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for document in payload.get("documents", []):
            item = parse_firestore_fields(document.get("fields", {}))
            item["id"] = document.get("name", "").split("/")[-1]
            if item.get("name") and (item.get("valueMin") is not None or item.get("valueMax") is not None or item.get("value") is not None):
                items.append(item)

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return items


async def get_mttvalues_items() -> list[dict]:
    now = time.time()
    cached_items = item_cache.get("items", [])
    updated_at = float(item_cache.get("updated_at", 0.0))

    if isinstance(cached_items, list) and cached_items and now - updated_at < ITEM_CACHE_SECONDS:
        return cached_items

    items = await asyncio.to_thread(fetch_mttvalues_items_sync)
    item_cache["items"] = items
    item_cache["updated_at"] = now
    return items


async def get_random_mttvalues_item() -> dict | None:
    try:
        items = await get_mttvalues_items()
    except Exception as error:
        print(f"Could not fetch MTTValues items: {error}")
        return None

    if not items:
        return None

    return random.choice(items)


def format_number(value: object) -> str:
    if value is None:
        return "N/A"

    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


def get_item_low_high(item: dict) -> tuple[object, object]:
    low = item.get("valueMin")
    high = item.get("valueMax")

    if low is None and high is None:
        low = item.get("value")
        high = item.get("value")
    elif low is None:
        low = high
    elif high is None:
        high = low

    return low, high


def create_vote_embed(
    color: int = GRAY_COLOR,
    counts: dict[str, int] | None = None,
    item: dict | None = None,
) -> discord.Embed:
    counts = counts or empty_vote_counts()

    if item:
        low, high = get_item_low_high(item)
        embed = discord.Embed(
            title=f"Value vote: {item.get('name', VOTE_TEXT)}",
            description=(
                "Should this value go up, stay the same, or go down?\n\n"
                f"{format_vote_counts(counts)}"
            ),
            color=discord.Color(color),
        )
        embed.add_field(name="Low", value=format_number(low), inline=True)
        embed.add_field(name="High", value=format_number(high), inline=True)

        demand = item.get("demand")
        if demand is not None:
            embed.add_field(name="Demand", value=str(demand), inline=True)

        rarity = item.get("rarity")
        if isinstance(rarity, list) and rarity:
            embed.add_field(name="Rarity", value=", ".join(str(value) for value in rarity), inline=False)

        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            embed.add_field(name="Tags", value=", ".join(str(value) for value in tags), inline=False)

        image = item.get("image")
        if isinstance(image, str) and image.startswith("http"):
            embed.set_thumbnail(url=image)

        embed.set_footer(text=f"{UPVOTE} raise value  {NEUTRAL_VOTE} keep value  {DOWNVOTE} lower value | Source: mttvalues.com")
        return embed

    return discord.Embed(
        title=VOTE_TEXT,
        description=format_vote_counts(counts),
        color=discord.Color(color),
    )


def get_vote_counts(message: discord.Message) -> dict[str, int]:
    counts = empty_vote_counts()

    for reaction in message.reactions:
        emoji = normalize_emoji(reaction.emoji)
        if emoji not in counts:
            continue

        bot_reaction = 1 if reaction.me else 0
        counts[emoji] = max(reaction.count - bot_reaction, 0)

    return counts


def get_vote_color(counts: dict[str, int]) -> int:
    upvotes = counts[normalize_emoji(UPVOTE)]
    neutral_votes = counts[normalize_emoji(NEUTRAL_VOTE)]
    downvotes = counts[normalize_emoji(DOWNVOTE)]

    if neutral_votes >= upvotes and neutral_votes >= downvotes:
        return GRAY_COLOR

    if upvotes == downvotes:
        return GRAY_COLOR

    if upvotes > downvotes:
        return GREEN_COLOR

    return RED_COLOR


async def update_vote_color(message: discord.Message) -> None:
    counts = get_vote_counts(message)
    color = get_vote_color(counts)
    embed = message.embeds[0].copy() if message.embeds else create_vote_embed()
    embed.color = discord.Color(color)
    if embed.description and "Should this value" in embed.description:
        embed.description = (
            "Should this value go up, stay the same, or go down?\n\n"
            f"{format_vote_counts(counts)}"
        )
    else:
        embed.description = format_vote_counts(counts)
    await message.edit(embed=embed)


async def send_vote(channel: discord.abc.Messageable) -> discord.Message:
    item = await get_random_mttvalues_item()
    message = await channel.send(embed=create_vote_embed(item=item))

    for reaction in VOTE_REACTIONS:
        await message.add_reaction(reaction)

    return message


@bot.tree.command(name="channel", description="Choose the channel for automatic votes.")
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
@app_commands.describe(
    amount="The number of seconds, minutes, or hours to wait between votes.",
    unit="The time unit for amount.",
)
@app_commands.choices(
    unit=[
        app_commands.Choice(name="seconds", value="seconds"),
        app_commands.Choice(name="minutes", value="minutes"),
        app_commands.Choice(name="hours", value="hours"),
    ]
)
async def time_command(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100000],
    unit: app_commands.Choice[str],
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

    interval_seconds = interval_to_seconds(amount, unit.value)
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


@bot.tree.command(name="votestart", description="Start the automatic vote timer.")
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


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await handle_vote_reaction(payload)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await handle_vote_reaction(payload)


async def handle_vote_reaction(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return

    if bot.user and payload.user_id == bot.user.id:
        return

    if normalize_emoji(payload.emoji) not in {normalize_emoji(reaction) for reaction in VOTE_REACTIONS}:
        return

    if not is_tracked_vote_message(payload.guild_id, payload.message_id):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.DiscordException:
            return

    if not hasattr(channel, "fetch_message"):
        return

    try:
        message = await channel.fetch_message(payload.message_id)
        await update_vote_color(message)
    except discord.DiscordException as error:
        print(f"Could not update vote color for message {payload.message_id}: {error}")


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
                message = await send_vote(channel)
            except discord.DiscordException as error:
                print(f"Could not send vote for guild {guild_id}: {error}")
                continue

            store_vote_message_id(guild_config, message.id)
            while float(next_vote_at) <= now:
                next_vote_at = float(next_vote_at) + int(interval_seconds)

            guild_config["next_vote_at"] = next_vote_at
            changed = True

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
