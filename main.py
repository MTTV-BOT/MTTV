from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()
discord.VoiceClient.warn_nacl = False

TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
PORT = int(os.getenv("PORT", 10000))
DEFAULT_DATA_DIR = "/app/data" if os.name != "nt" else "."
DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
CACHE = os.path.join(DATA_DIR, "price_list.json")
SOURCE_SITE = "https://mttvalues.com/"
FIRESTORE_BASE_URL = (
    "https://firestore.googleapis.com/v1/"
    "projects/military-tycoon-trading-values/databases/(default)/documents/items"
)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
REFRESH_INTERVAL_SECONDS = 3600
REFRESH_STALE_AFTER_SECONDS = 3600
MAX_AUTOCOMPLETE_CHOICES = 25

RARITY_STYLE_MAP: dict[str, discord.ButtonStyle] = {
    "Common": discord.ButtonStyle.primary,
    "Rare": discord.ButtonStyle.primary,
    "Legendary": discord.ButtonStyle.success,
    "Epic": discord.ButtonStyle.primary,
    "Exotic": discord.ButtonStyle.success,
    "Limited": discord.ButtonStyle.danger,
}

TAG_STYLE_MAP: dict[str, discord.ButtonStyle] = {
    "rising": discord.ButtonStyle.success,
    "stable": discord.ButtonStyle.primary,
    "dropping": discord.ButtonStyle.danger,
    "underpaid": discord.ButtonStyle.success,
    "overpaid": discord.ButtonStyle.success,
    "meta": discord.ButtonStyle.primary,
    "unstable": discord.ButtonStyle.danger,
}

RARITY_EMOJI_MAP: dict[str, str] = {
    "Common": "⚪",
    "Rare": "🔵",
    "Legendary": "🟡",
    "Epic": "🟣",
    "Exotic": "🟣",
    "Limited": "🔴",
}

TAG_EMOJI_MAP: dict[str, str] = {
    "rising": "🟢",
    "stable": "🔵",
    "dropping": "🔴",
    "underpaid": "🟢",
    "overpaid": "🟠",
    "meta": "🟣",
    "unstable": "🔴",
}

KNOWN_FILTER_TERMS = set(TAG_STYLE_MAP) | {rarity.lower() for rarity in RARITY_STYLE_MAP}

items_cache: list[dict[str, Any]] = []
item_lookup: dict[str, dict[str, Any]] = {}
search_entries: list[dict[str, Any]] = []
last_refresh_at: str | None = None


def normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def compact_text(text: str) -> str:
    return normalize_text(text).replace(" ", "")


def value_rank(item: dict[str, Any]) -> int:
    values = [item.get("value_max"), item.get("value_min")]
    numeric_values = [int(v) for v in values if isinstance(v, int)]
    return max(numeric_values) if numeric_values else 0


def average_value(item: dict[str, Any]) -> int | None:
    value_min = item.get("value_min")
    value_max = item.get("value_max")
    if isinstance(value_min, int) and isinstance(value_max, int):
        return round((value_min + value_max) / 2)
    if isinstance(value_min, int):
        return value_min
    if isinstance(value_max, int):
        return value_max
    return None


def format_number(value: int | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,}"


def format_value_range(item: dict[str, Any]) -> str:
    value_min = item.get("value_min")
    value_max = item.get("value_max")
    if isinstance(value_min, int) and isinstance(value_max, int):
        if value_min == value_max:
            return format_number(value_min)
        return f"{format_number(value_min)} - {format_number(value_max)}"
    if isinstance(value_min, int):
        return format_number(value_min)
    if isinstance(value_max, int):
        return format_number(value_max)
    return "No value listed"


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def decode_firestore_value(raw: dict[str, Any]) -> Any:
    if "stringValue" in raw:
        return raw["stringValue"]
    if "integerValue" in raw:
        return int(raw["integerValue"])
    if "doubleValue" in raw:
        return float(raw["doubleValue"])
    if "booleanValue" in raw:
        return bool(raw["booleanValue"])
    if "nullValue" in raw:
        return None
    if "timestampValue" in raw:
        return raw["timestampValue"]
    if "arrayValue" in raw:
        values = raw["arrayValue"].get("values", [])
        return [decode_firestore_value(value) for value in values]
    if "mapValue" in raw:
        fields = raw["mapValue"].get("fields", {})
        return {key: decode_firestore_value(value) for key, value in fields.items()}
    return None


def extract_aliases(name: str) -> set[str]:
    aliases: set[str] = set()
    normalized_name = normalize_text(name)
    compact_name = compact_text(name)

    if normalized_name:
        aliases.add(normalized_name)
    if compact_name:
        aliases.add(compact_name)

    no_parentheses = normalize_text(re.sub(r"\([^)]*\)", " ", name))
    if no_parentheses:
        aliases.add(no_parentheses)
        aliases.add(no_parentheses.replace(" ", ""))

    for part in re.findall(r"\(([^)]+)\)", name):
        normalized_part = normalize_text(part)
        compact_part = compact_text(part)
        if normalized_part:
            aliases.add(normalized_part)
        if compact_part:
            aliases.add(compact_part)

    return {alias for alias in aliases if alias}


def normalize_item(raw_item: dict[str, Any]) -> dict[str, Any]:
    tags = [str(tag) for tag in raw_item.get("tags", []) if str(tag).strip()]
    rarity = [str(entry) for entry in raw_item.get("rarity", []) if str(entry).strip()]

    item = {
        "id": str(raw_item.get("id", "")).strip(),
        "name": str(raw_item.get("name", "Unknown Item")).strip() or "Unknown Item",
        "description": str(raw_item.get("description", "")).strip(),
        "image": str(raw_item.get("image", "")).strip(),
        "value_min": raw_item.get("value_min"),
        "value_max": raw_item.get("value_max"),
        "demand": raw_item.get("demand"),
        "functionality": raw_item.get("functionality"),
        "tags": tags,
        "rarity": rarity,
        "updated_at": raw_item.get("updated_at"),
        "created_at": raw_item.get("created_at"),
    }

    if not isinstance(item["value_min"], int):
        item["value_min"] = None
    if not isinstance(item["value_max"], int):
        item["value_max"] = None
    if not isinstance(item["demand"], int):
        item["demand"] = None
    if not isinstance(item["functionality"], int):
        item["functionality"] = None

    return item


def rebuild_indexes(items: list[dict[str, Any]], refreshed_at: str | None = None) -> None:
    global items_cache, item_lookup, search_entries, last_refresh_at

    normalized_items = [normalize_item(item) for item in items if item.get("id")]
    normalized_items.sort(key=lambda item: (-value_rank(item), item["name"].lower()))

    new_lookup = {item["id"]: item for item in normalized_items}
    new_entries: list[dict[str, Any]] = []

    for item in normalized_items:
        name_norm = normalize_text(item["name"])
        name_aliases = extract_aliases(item["name"])
        filter_terms = {
            normalize_text(tag)
            for tag in [*item.get("tags", []), *item.get("rarity", [])]
            if normalize_text(tag)
        }
        new_entries.append(
            {
                "item": item,
                "name_norm": name_norm,
                "name_compact": name_norm.replace(" ", ""),
                "name_aliases": name_aliases,
                "filter_terms": filter_terms,
                "score_value": value_rank(item),
            }
        )

    items_cache = normalized_items
    item_lookup = new_lookup
    search_entries = new_entries
    last_refresh_at = refreshed_at or datetime.now(timezone.utc).isoformat()


def _save_cache_sync(payload: dict[str, Any]) -> None:
    with open(CACHE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


async def save_cache() -> None:
    payload = {
        "source": SOURCE_SITE,
        "fetched_at": last_refresh_at,
        "items": items_cache,
    }
    await asyncio.to_thread(_save_cache_sync, payload)


def load_cache() -> None:
    try:
        with open(CACHE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        logging.info("No cache file found yet.")
        return
    except Exception as exc:
        logging.error("Failed to load cache: %s", exc)
        return

    cached_items = payload.get("items")
    if not isinstance(cached_items, list):
        logging.info("Existing cache format is legacy or invalid. It will be replaced on refresh.")
        return

    rebuild_indexes(cached_items, payload.get("fetched_at"))
    logging.info("Loaded %s items from cache.", len(items_cache))


async def fetch_firestore_documents(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    next_page_token: str | None = None

    while True:
        params = {"pageSize": "1000"}
        if next_page_token:
            params["pageToken"] = next_page_token

        async with session.get(
            FIRESTORE_BASE_URL,
            params=params,
            ssl=False,
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Firestore request failed ({response.status}): {text[:300]}")

            payload = await response.json()

        documents.extend(payload.get("documents", []))
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            return documents


def transform_firestore_document(document: dict[str, Any]) -> dict[str, Any]:
    fields = document.get("fields", {})
    parsed_fields = {key: decode_firestore_value(value) for key, value in fields.items()}
    return {
        "id": str(document.get("name", "")).rsplit("/", 1)[-1],
        "name": parsed_fields.get("name", "Unknown Item"),
        "description": parsed_fields.get("description", ""),
        "image": parsed_fields.get("image", ""),
        "value_min": parsed_fields.get("valueMin"),
        "value_max": parsed_fields.get("valueMax"),
        "demand": parsed_fields.get("demand"),
        "functionality": parsed_fields.get("functionality"),
        "tags": parsed_fields.get("tags", []),
        "rarity": parsed_fields.get("rarity", []),
        "updated_at": document.get("updateTime"),
        "created_at": document.get("createTime"),
    }


async def download_live_items() -> list[dict[str, Any]]:
    headers = {"User-Agent": "Mozilla/5.0 (Discord Value Bot)"}
    async with aiohttp.ClientSession(headers=headers, timeout=REQUEST_TIMEOUT) as session:
        documents = await fetch_firestore_documents(session)
    return [transform_firestore_document(document) for document in documents]


def search_matches(query: str, limit: int = 5) -> list[dict[str, Any]]:
    normalized_query = normalize_text(query)
    compact_query = normalized_query.replace(" ", "")

    if not normalized_query:
        return [entry["item"] for entry in search_entries[:limit]]

    exact_name_matches = []
    startswith_matches = []
    contains_matches = []
    filter_matches = []

    for entry in search_entries:
        aliases = entry["name_aliases"]
        if normalized_query == entry["name_norm"] or compact_query == entry["name_compact"] or normalized_query in aliases:
            exact_name_matches.append(entry)
            continue
        if entry["name_norm"].startswith(normalized_query) or any(
            alias.startswith(normalized_query) for alias in aliases
        ):
            startswith_matches.append(entry)
            continue
        if normalized_query in entry["name_norm"] or any(normalized_query in alias for alias in aliases):
            contains_matches.append(entry)
            continue
        if normalized_query in entry["filter_terms"]:
            filter_matches.append(entry)

    ordered_entries = exact_name_matches or startswith_matches or contains_matches or filter_matches
    if ordered_entries:
        ordered_entries.sort(key=lambda entry: (-entry["score_value"], entry["item"]["name"].lower()))
        return [entry["item"] for entry in ordered_entries[:limit]]

    fuzzy_matches = [
        (
            difflib.SequenceMatcher(None, normalized_query, entry["name_norm"]).ratio(),
            entry,
        )
        for entry in search_entries
    ]
    fuzzy_matches = [pair for pair in fuzzy_matches if pair[0] >= 0.62]
    fuzzy_matches.sort(key=lambda pair: (-pair[0], -pair[1]["score_value"], pair[1]["item"]["name"].lower()))
    return [entry["item"] for _, entry in fuzzy_matches[:limit]]


def resolve_item(query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    raw_query = (query or "").strip()
    if not raw_query:
        return None, []

    direct_match = item_lookup.get(raw_query)
    if direct_match:
        return direct_match, []

    normalized_query = normalize_text(raw_query)
    compact_query = normalized_query.replace(" ", "")
    if not normalized_query:
        return None, []

    exact_matches = []
    startswith_matches = []
    contains_matches = []

    for entry in search_entries:
        aliases = entry["name_aliases"]
        if normalized_query == entry["name_norm"] or compact_query == entry["name_compact"] or normalized_query in aliases:
            exact_matches.append(entry["item"])
        elif entry["name_norm"].startswith(normalized_query) or any(
            alias.startswith(normalized_query) for alias in aliases
        ):
            startswith_matches.append(entry["item"])
        elif normalized_query in entry["name_norm"] or any(normalized_query in alias for alias in aliases):
            contains_matches.append(entry["item"])

    for matches in (exact_matches, startswith_matches, contains_matches):
        unique_matches = list({item["id"]: item for item in matches}.values())
        if len(unique_matches) == 1:
            return unique_matches[0], []
        if len(unique_matches) > 1:
            return None, unique_matches[:5]

    if normalized_query in KNOWN_FILTER_TERMS:
        return None, search_matches(raw_query, limit=5)

    fuzzy_scores = []
    for entry in search_entries:
        ratio = difflib.SequenceMatcher(None, normalized_query, entry["name_norm"]).ratio()
        if ratio >= 0.72:
            fuzzy_scores.append((ratio, entry["item"]))

    fuzzy_scores.sort(key=lambda pair: (-pair[0], -value_rank(pair[1]), pair[1]["name"].lower()))
    if len(fuzzy_scores) == 1:
        return fuzzy_scores[0][1], []
    if len(fuzzy_scores) >= 2:
        best_ratio, best_item = fuzzy_scores[0]
        next_ratio = fuzzy_scores[1][0]
        if best_ratio >= 0.84 and best_ratio - next_ratio >= 0.08:
            return best_item, []
        return None, [item for _, item in fuzzy_scores[:5]]

    return None, search_matches(raw_query, limit=5)


def truncate_choice_label(text: str, max_length: int = 100) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def build_choice_label(item: dict[str, Any]) -> str:
    label = item["name"]
    rarity = item.get("rarity") or []
    if rarity:
        label = f"{label} [{rarity[0]}]"
    return truncate_choice_label(label)


def rarity_color(item: dict[str, Any]) -> discord.Color:
    rarity = item.get("rarity") or []
    primary = rarity[0] if rarity else None
    if primary == "Limited":
        return discord.Color.red()
    if primary == "Exotic":
        return discord.Color.purple()
    if primary == "Legendary":
        return discord.Color.gold()
    if primary == "Rare":
        return discord.Color.blue()
    if primary == "Common":
        return discord.Color.light_grey()
    return discord.Color.blurple()


def chip_label(chip_type: str, value: str) -> str:
    if chip_type == "rarity":
        emoji = RARITY_EMOJI_MAP.get(value, "🔹")
        return f"{emoji} {value}"

    normalized = value.lower()
    emoji = TAG_EMOJI_MAP.get(normalized, "🔹")
    return f"{emoji} {value.title()}"


class ItemTagView(discord.ui.View):
    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(timeout=43200)

        row = 0
        column = 0
        chips = []
        chips.extend(("rarity", value) for value in item.get("rarity", []))
        chips.extend(("tag", value) for value in item.get("tags", []))

        for chip_type, value in chips[:20]:
            if column == 5:
                row += 1
                column = 0
            if row >= 4:
                break

            style = (
                RARITY_STYLE_MAP.get(value, discord.ButtonStyle.primary)
                if chip_type == "rarity"
                else TAG_STYLE_MAP.get(value.lower(), discord.ButtonStyle.primary)
            )
            button_label = chip_label(chip_type, value)
            chip_key = normalize_text(value).replace(" ", "-") or "chip"
            self.add_item(
                PassiveTagButton(
                    label=button_label,
                    style=style,
                    row=row,
                    custom_id=f"value-chip:{chip_type}:{chip_key}",
                )
            )
            column += 1


class PassiveTagButton(discord.ui.Button["ItemTagView"]):
    def __init__(
        self,
        *,
        label: str,
        style: discord.ButtonStyle,
        row: int,
        custom_id: str,
    ) -> None:
        super().__init__(label=label, style=style, row=row, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


def build_item_embed(item: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=item["name"],
        color=rarity_color(item),
        url=SOURCE_SITE,
    )

    embed.add_field(name="Value Range", value=format_value_range(item), inline=True)
    embed.add_field(name="Average Value", value=format_number(average_value(item)), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Demand", value=f'{item["demand"]}/10' if item.get("demand") is not None else "N/A", inline=True)
    embed.add_field(
        name="Functionality",
        value=f'{item["functionality"]}/10' if item.get("functionality") is not None else "N/A",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if item.get("image", "").startswith("http"):
        embed.set_image(url=item["image"])

    updated_at = parse_iso_datetime(item.get("updated_at")) or parse_iso_datetime(last_refresh_at)
    if updated_at:
        embed.timestamp = updated_at
        footer = "Source: mttvalues.com | Last updated"
    else:
        footer = "Source: mttvalues.com"
    embed.set_footer(text=footer)
    return embed


class H(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_args: Any) -> None:
        return


def start_health_server() -> None:
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), H).serve_forever(),
        daemon=True,
    ).start()
    logging.info("Health check server started on port %s", PORT)


load_cache()


class Bot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents, activity=discord.Game(name="/value | mttvalues.com"))
        self.tree = app_commands.CommandTree(self)
        self.refresh_lock = asyncio.Lock()
        self.loop_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_app_command_error
        await self.tree.sync()
        logging.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logging.info("Slash commands synced.")
        self.loop_task = asyncio.create_task(self.refresh_loop())
        asyncio.create_task(self.refresh_values())

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            try:
                await self.refresh_values()
            except Exception:
                logging.exception("Background refresh failed")

    async def refresh_values(self) -> int:
        async with self.refresh_lock:
            if items_cache and last_refresh_at:
                refreshed_at = parse_iso_datetime(last_refresh_at)
                if refreshed_at and (
                    datetime.now(timezone.utc) - refreshed_at
                ).total_seconds() < REFRESH_STALE_AFTER_SECONDS:
                    return len(items_cache)

            live_items = await download_live_items()
            rebuild_indexes(live_items, datetime.now(timezone.utc).isoformat())
            await save_cache()
            logging.info("Refreshed %s items from mttvalues.com", len(items_cache))
            return len(items_cache)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You need to be a server admin to use this command."
        else:
            logging.error("Command error: %s", error)
            message = "Something went wrong while running that command."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logging.exception("Failed to send error response")

    async def close(self) -> None:
        if self.loop_task:
            self.loop_task.cancel()
        await super().close()


bot = Bot()


@bot.tree.command(name="value", description="Check a Military Tycoon item value from mttvalues.com")
@app_commands.describe(item="Item name, short name, or abbreviation")
async def value(interaction: discord.Interaction, item: str) -> None:
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        logging.error("Interaction expired before the bot could defer")
        return

    result, suggestions = resolve_item(item)

    if result is None and not items_cache:
        try:
            await bot.refresh_values()
        except Exception:
            logging.exception("On-demand refresh failed while resolving an item")
        result, suggestions = resolve_item(item)

    if result is None:
        if not suggestions:
            await interaction.followup.send(
                f'No item matched "{item}". Try using the autocomplete list from `/value`.',
                ephemeral=True,
            )
            return

        suggestion_lines = "\n".join(f"- {suggestion['name']}" for suggestion in suggestions[:5])
        await interaction.followup.send(
            f'I found multiple close matches for "{item}". Try one of these:\n{suggestion_lines}',
            ephemeral=True,
        )
        return

    embed = build_item_embed(result)
    view = ItemTagView(result)
    await interaction.followup.send(embed=embed, view=view)


@value.autocomplete("item")
async def value_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    matches = search_matches(current, limit=MAX_AUTOCOMPLETE_CHOICES)
    return [
        app_commands.Choice(name=build_choice_label(item), value=item["id"])
        for item in matches[:MAX_AUTOCOMPLETE_CHOICES]
    ]

if not TOKEN:
    logging.error("Token missing in .env")
    raise SystemExit(1)


async def run_with_backoff() -> None:
    backoff = 15
    while True:
        try:
            await bot.start(TOKEN, reconnect=True)
            return
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logging.error("Discord rate limit while starting. Backing off for %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 600)
                continue

            logging.exception("Discord HTTP exception during startup")
        except Exception:
            logging.exception("Unexpected startup error")

        await asyncio.sleep(min(backoff, 60))
        backoff = min(backoff * 2, 600)


if __name__ == "__main__":
    start_health_server()
    asyncio.run(run_with_backoff())
