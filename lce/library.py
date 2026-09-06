"""World Library — a fast scan of one or more saves folders into gallery entries
(name + platform + thumbnail + title update), reusing the converter engine's cheap
metadata readers (no decompression). Title update comes free from the folder path in
a Nexia/emulator library (``.../NO_TU/...`` or ``.../TU19/...``); otherwise it's left
blank and can be filled in lazily with ``describe`` on demand.

    from lce import library as L
    worlds = L.scan(["C:/Nexia360/Library"])       # [{path,name,platform,thumbnail,tu,detail}]
"""
import os
import re
from pathlib import Path

_TU_RE = re.compile(r"(?:^|[\\/])(NO_TU|TU\d+)(?:[\\/]|$)", re.I)

# how a platform code shows on a card
PLATFORM_LABEL = {"xbox360": "Xbox 360", "ps3": "PS3", "windows_lce": "Windows LCE"}


def tu_from_path(p):
    """The title update named by a Nexia/emulator library path, or None."""
    m = _TU_RE.search(str(p))
    if not m:
        return None
    t = m.group(1).upper()
    return "TU0" if t == "NO_TU" else t


def _clean_name(name):
    n = str(name or "").strip()
    if n.lower().endswith(".bin"):
        n = n[:-4]
    return n or "world"


def scan(folders, log=None, max_depth=8):
    """Walk each folder (bounded depth) and return a de-duplicated, name-sorted list of
    world entries: {path, name, platform, thumbnail (PNG bytes|None), tu (str|None),
    detail}. Fast — only names/thumbnails are read, nothing is decompressed."""
    from .converter import lce_engine as E
    out, seen = [], set()

    def add(container, name, plat, thumb, tu, detail):
        key = os.path.normcase(str(container))
        if key in seen:
            return
        seen.add(key)
        out.append({"path": str(container), "name": _clean_name(name), "platform": plat,
                    "thumbnail": thumb, "tu": tu, "detail": detail})

    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            try:
                depth = len(d.relative_to(root).parts)
            except ValueError:
                depth = 0
            if depth >= max_depth:
                dirnames[:] = []
            tu = tu_from_path(d)
            fset = set(filenames)
            if E.EMU_DAT_NAME in fset:                          # emulator world (savegame.dat)
                add(d / E.EMU_DAT_NAME, E.read_worldname_txt(d) or d.name, "xbox360",
                    E.find_thumbnail(d, E.EMU_THUMB_NAME), tu, "Xbox 360 (emulator)")
            if "saveData.ms" in fset:                           # Windows LCE world
                add(d / "saveData.ms", E.read_worldname_txt(d) or d.name, "windows_lce",
                    E.find_thumbnail(d, E.WIN64_THUMB_REL), tu, "Windows LCE")
            if "GAMEDATA" in fset:                              # PS3 save
                add(d / "GAMEDATA", d.name, "ps3", E.find_thumbnail(d, "THUMB"), tu, "PS3")
            for f in filenames:                                 # loose .bin STFS packages
                if f.lower().endswith(".bin"):
                    b = d / f
                    try:
                        nm, th = E.peek_stfs_metadata(b)
                    except Exception:
                        nm, th = None, None
                    if nm is not None or th is not None:
                        add(b, nm or b.stem, "xbox360", th, tu, "Xbox 360 (.bin)")
            if log:
                log("scanning %s\u2026 (%d worlds)" % (d.name, len(out)))
    out.sort(key=lambda w: (w["name"] or "").lower())
    return out


def describe(path, platform):
    """Slower per-world detail (title-update range, size, dimensions, seed) for a card,
    read on demand. Returns a dict or None on failure."""
    try:
        from .converter import lce_engine as E
        payload, _name, _thumb = E.read_console_input(path, platform)
        return E.describe_world(payload, ">", platform)   # console payloads are big-endian
    except Exception:
        return None
