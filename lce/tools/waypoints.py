"""Waypoints — a personal atlas of named coordinates per world.

Stored OUTSIDE the save (never touches game data) at
``%APPDATA%/LCEStudio/waypoints/<key>.json`` (key = hash of the save path). Each
waypoint is ``{name, x, y, z, dim, color, note}`` where dim is 0/-1/1
(Overworld/Nether/End). The GUI draws them as pins on the map and teleports
players to them.

    from lce import waypoints as WP
    wps = WP.load(save_path)
    wps.append({"name": "Base", "x": 0, "y": 64, "z": 0, "dim": 0, "color": "#39c", "note": ""})
    WP.store(save_path, wps)
"""
import hashlib
import json
import os

DIM_NAME = {0: "Overworld", -1: "Nether", 1: "End"}
COLORS = ["#39c0ff", "#ffd24a", "#7ee787", "#ff7b72", "#d2a8ff", "#ffa657", "#79c0ff"]


def _dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "LCEStudio", "waypoints")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _path(save_path):
    key = hashlib.sha1(os.path.normcase(os.path.abspath(str(save_path))).encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(_dir(), key + ".json")


def load(save_path):
    try:
        with open(_path(save_path), encoding="utf-8") as f:
            wps = json.load(f).get("waypoints", [])
        return [w for w in wps if isinstance(w, dict) and "x" in w]
    except Exception:
        return []


def store(save_path, waypoints):
    # Write to a temp file then atomically replace, so a failure mid-write can never
    # truncate/corrupt an existing waypoints file (the "w" mode would empty it first).
    dst = _path(save_path)
    tmp = dst + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"source": str(save_path), "waypoints": waypoints}, f, indent=2)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
