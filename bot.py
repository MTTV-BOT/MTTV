"""MTTV bot entry point.

The bot core is shared, while value lookup and voting are optional feature
modules. If either feature file is removed, the other feature still starts.
"""

import importlib

from mttv_shared import TOKEN, bot, print, start_health_server

OPTIONAL_FEATURE_MODULES = (
    "mttv_value",
)


def load_optional_feature(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            print(f"Optional feature module {module_name!r} is missing; continuing without it.")
            return
        raise


for feature_module in OPTIONAL_FEATURE_MODULES:
    load_optional_feature(feature_module)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")

    start_health_server()
    bot.run(TOKEN)
