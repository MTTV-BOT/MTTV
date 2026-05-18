import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


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

intents = discord.Intents.default()
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)


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
        updates["next_vote_at"] = time.time() + interval_seconds

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


def format_vote_counts(counts: dict[str, int]) -> str:
    return (
        f"{UPVOTE} {counts[normalize_emoji(UPVOTE)]}   "
        f"{NEUTRAL_VOTE} {counts[normalize_emoji(NEUTRAL_VOTE)]}   "
        f"{DOWNVOTE} {counts[normalize_emoji(DOWNVOTE)]}"
    )


def create_vote_embed(color: int = GRAY_COLOR, counts: dict[str, int] | None = None) -> discord.Embed:
    return discord.Embed(
        title=VOTE_TEXT,
        description=format_vote_counts(counts or empty_vote_counts()),
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
    embed.description = format_vote_counts(counts)
    await message.edit(embed=embed)


async def send_vote(channel: discord.abc.Messageable) -> discord.Message:
    message = await channel.send(embed=create_vote_embed())

    for reaction in VOTE_REACTIONS:
        await message.add_reaction(reaction)

    return message


@bot.tree.command(name="channel", description="Choose the channel for automatic votes.")
@app_commands.describe(channel="The channel where new votes should be posted.")
@app_commands.default_permissions(manage_guild=True)
async def channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
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
@app_commands.default_permissions(manage_guild=True)
async def time_command(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100000],
    unit: app_commands.Choice[str],
):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
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
    set_guild_config(
        interaction.guild.id,
        {
            "interval_seconds": interval_seconds,
            "next_vote_at": time.time() + interval_seconds,
        },
    )

    await interaction.response.send_message(
        f"New votes will be posted in {channel.mention} every {format_interval(interval_seconds)}.",
        ephemeral=True,
    )


@bot.tree.command(name="vote_now", description="Post a vote immediately in the saved channel.")
@app_commands.default_permissions(manage_guild=True)
async def vote_now(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
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

    message = await send_vote(channel)
    remember_vote_message(interaction.guild.id, message.id)
    await interaction.response.send_message(f"Vote posted in {channel.mention}.", ephemeral=True)


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

            if not channel_id or not interval_seconds or not next_vote_at:
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
async def setup_hook():
    if GUILD_ID:
        try:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            guild_commands = await bot.tree.sync(guild=guild)
            print(f"Synced {len(guild_commands)} guild command(s) for {GUILD_ID}.")
        except ValueError:
            print("GUILD_ID must be a number. Syncing global commands instead.")
        except discord.DiscordException as error:
            print(f"Guild command sync failed: {error}")

    try:
        global_commands = await bot.tree.sync()
        print(f"Synced {len(global_commands)} global command(s).")
    except discord.DiscordException as error:
        print(f"Global command sync failed: {error}")

    bot.loop.create_task(vote_worker())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")

    start_health_server()
    bot.run(TOKEN)
