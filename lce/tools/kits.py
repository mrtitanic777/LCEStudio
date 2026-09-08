"""Loadout kits — named inventories you can save once and apply to any player.

A kit is just a list of item dicts (id/count/damage/slot + name/lore/enchants/potion/
effects, the same shape World.capture_inventory produces), stored OUTSIDE any save at
``%APPDATA%/LCEStudio/kits.json`` so kits move between worlds.

    from lce.tools import kits
    kits.save_kit("PvP", world.capture_inventory())      # capture the current inventory
    kits.apply(world, kits.get("PvP"), players="all")    # give it to every player
    world.save(...)
"""
import json
import os


def _path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "LCEStudio")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "kits.json")


def load_all():
    """{name: [item dict, ...]} of every saved kit."""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.get("kits", {}).items() if isinstance(v, list)}
    except Exception:
        return {}


def save_all(kits):
    """Persist the whole kit dict atomically (temp file + os.replace)."""
    p = _path(); tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"kits": kits}, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def list_kits():
    return sorted(load_all())


def get(name):
    return load_all().get(name)


def save_kit(name, items):
    """Save (or overwrite) a named kit from a list of item dicts."""
    kits = load_all()
    kits[name] = list(items)
    save_all(kits)
    return name


def delete_kit(name):
    kits = load_all()
    if name in kits:
        del kits[name]
        save_all(kits)
        return True
    return False


def apply(world, items, players="active", replace=True):
    """Write a kit's items into player inventories. players: 'active' (the loaded
    profile), 'all', or a list of player keys. Returns the number of players changed.
    The world still needs saving to write it to disk."""
    if players == "all":
        targets = world.players()
    elif players == "active":
        targets = [world._pkey]
    else:
        targets = list(players)
    for pkey in targets:
        world.set_player_inventory(pkey, items, replace=replace)
    return len(targets)
