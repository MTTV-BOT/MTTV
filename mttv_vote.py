"""Voting feature for the MTTV bot.

This module can be removed without breaking the /value feature.
"""

from mttv_shared import *

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
        return

    choice = choice_from_emoji(payload.emoji)
    if choice is None:
        return

    guild_config = get_guild_config(payload.guild_id)
    if not is_tracked_vote_message(guild_config, payload.message_id):
        return

    try:
        message = await fetch_vote_message(payload.channel_id, payload.message_id)
    except discord.DiscordException as error:
        print(f"Could not fetch vote message for reaction add: {error}")
        return

    if message is None:
        return

    counts = set_reaction_vote(payload.guild_id, payload.message_id, payload.user_id, choice)

    try:
        await remove_other_vote_reactions(message, payload, choice)
    except discord.DiscordException as error:
        print(f"Could not clean old vote reactions: {error}")

    try:
        await update_vote_message_embed(message, counts)
    except discord.DiscordException as error:
        print(f"Could not update vote message color: {error}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
        return

    choice = choice_from_emoji(payload.emoji)
    if choice is None:
        return

    guild_config = get_guild_config(payload.guild_id)
    if not is_tracked_vote_message(guild_config, payload.message_id):
        return

    counts = remove_reaction_vote(payload.guild_id, payload.message_id, payload.user_id, choice)

    try:
        message = await fetch_vote_message(payload.channel_id, payload.message_id)
    except discord.DiscordException as error:
        print(f"Could not fetch vote message for reaction remove: {error}")
        return

    if message is None:
        return

    try:
        await update_vote_message_embed(message, counts)
    except discord.DiscordException as error:
        print(f"Could not update vote message color: {error}")


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
async def voteforce(
    interaction: discord.Interaction,
    vehicle_name: str,
):
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

STARTUP_TASKS.append(vote_worker)
