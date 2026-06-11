"""Value lookup feature for the MTTV bot.

This module can be removed without breaking the vote feature.
"""

from mttv_shared import *

@bot.tree.command(name="value", description="Check a Military Tycoon item value from mttvalues.com.")
@app_commands.describe(item="The item name to look up.")
async def value(interaction: discord.Interaction, item: str):
    deferred = await safe_defer_interaction(interaction)

    try:
        items = await get_mttvalues_items()
    except Exception as error:
        print(f"Could not fetch MTT Values items for /value: {error}")
        await send_interaction_result(
            interaction,
            content="Could not fetch mttvalues.com right now.",
            ephemeral=True,
            deferred=deferred,
        )
        return

    matched_item = find_mttvalues_item(items, item)
    if matched_item is None:
        suggestions = match_vehicle_names(unique_vehicle_names(items), item)
        if suggestions:
            suggestion_text = "\n".join(f"- {name}" for name in suggestions[:5])
            await send_interaction_result(
                interaction,
                content=f"Could not find `{item}`. Did you mean:\n{suggestion_text}",
                ephemeral=True,
                deferred=deferred,
            )
        else:
            await send_interaction_result(
                interaction,
                content=f"Could not find `{item}` on mttvalues.com.",
                ephemeral=True,
                deferred=deferred,
            )
        return

    value_image = await create_large_value_image_file(matched_item)
    value_embed = create_value_embed(matched_item, use_attached_image=value_image is not None)
    if value_image is not None:
        await send_interaction_result(interaction, embed=value_embed, file=value_image, deferred=deferred)
    else:
        await send_interaction_result(interaction, embed=value_embed, deferred=deferred)


@value.autocomplete("item")
async def value_item_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    names = await get_mttvalues_autocomplete_names()
    return [
        app_commands.Choice(name=truncate_choice_text(name), value=truncate_choice_text(name))
        for name in match_vehicle_names(names, current)
    ]
