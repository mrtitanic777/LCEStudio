"""World Timelapse — keep a rolling history of a save and scrub through it.

A snapshot is just a copy of the world's ``savegame.dat`` taken each time it is saved,
stored under ``%APPDATA%/LCEStudio/timelapse/<key>/NNNN/savegame.dat`` (key = a hash of
the save path) with a ``meta.json`` index. ``frame`` renders (and caches) an isometric
PNG of a snapshot for the scrub slider; ``restore`` copies an old snapshot back over the
live save — a visual "undo across sessions".

    from lce import timelapse as TL
    TL.snapshot(save_path)                 # call after each save
    snaps = TL.list_snapshots(save_path)   # [{index, time, dir}]
    png = TL.frame(save_path, idx)         # rendered iso PNG (cached)
    TL.restore(save_path, idx)             # roll the live save back to a snapshot
"""
import hashlib
import json
import os
import shutil
import time


def _appdir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "LCEStudio", "timelapse")
    os.makedirs(d, exist_ok=True)
    return d


def _key(save_path):
    p = os.path.normcase(os.path.abspath(str(save_path)))
    return hashlib.sha1(p.encode("utf-8", "replace")).hexdigest()[:16]


def _wdir(save_path):
    d = os.path.join(_appdir(), _key(save_path))
    os.makedirs(d, exist_ok=True)
    return d


def _find_dat(save_path):
    """The live savegame.dat for a save path (a folder, or the file itself)."""
    p = str(save_path)
    if os.path.isfile(p) and os.path.basename(p).lower() == "savegame.dat":
        return p
    if os.path.isdir(p):
        dat = os.path.join(p, "savegame.dat")
        if os.path.exists(dat):
            return dat
    return None


def _meta_path(d):
    return os.path.join(d, "meta.json")


def _load_meta(d):
    try:
        with open(_meta_path(d), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"snapshots": []}


def _save_meta(d, meta):
    try:
        with open(_meta_path(d), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def list_snapshots(save_path):
    return _load_meta(_wdir(save_path)).get("snapshots", [])


def snapshot(save_path, max_keep=40):
    """Copy the current savegame.dat into the history. Returns the new snapshot dict, or
    None if there is no savegame.dat (e.g. a save that only lives as a .bin package)."""
    dat = _find_dat(save_path)
    if not dat:
        return None
    d = _wdir(save_path)
    meta = _load_meta(d)
    snaps = meta.get("snapshots", [])
    idx = (snaps[-1]["index"] + 1) if snaps else 0
    sub = os.path.join(d, "%04d" % idx)
    os.makedirs(sub, exist_ok=True)
    shutil.copy2(dat, os.path.join(sub, "savegame.dat"))
    snaps.append({"index": idx, "time": time.time(), "dir": sub})
    while len(snaps) > max_keep:                       # drop the oldest
        old = snaps.pop(0)
        shutil.rmtree(old.get("dir", ""), ignore_errors=True)
    meta["snapshots"] = snaps
    meta["source"] = str(save_path)
    _save_meta(d, meta)
    return snaps[-1]


def _snap(save_path, index):
    for s in _load_meta(_wdir(save_path)).get("snapshots", []):
        if s["index"] == index:
            return s
    return None


def frame(save_path, index, tile=8, log=None):
    """Render (and cache) an isometric PNG of a snapshot. Returns the PNG path or None."""
    s = _snap(save_path, index)
    if not s:
        return None
    png = os.path.join(s["dir"], "iso.png")
    if os.path.exists(png):
        return png
    from ..view3d.world import World as VW
    from .. import iso
    vw = VW.load(s["dir"], progress=None)              # the snapshot dir holds savegame.dat
    img = iso.render_iso(vw, tile=tile, log=log)
    img.save(png)
    return png


def restore(save_path, index):
    """Copy snapshot `index` back over the live savegame.dat (a .bak is kept). Returns the
    restored path. Only works where the save is a folder/emulator save (has savegame.dat)."""
    dat = _find_dat(save_path)
    if not dat:
        raise ValueError("this save has no plain savegame.dat to restore into")
    s = _snap(save_path, index)
    if not s:
        raise ValueError("no such snapshot")
    src = os.path.join(s["dir"], "savegame.dat")
    bak = dat + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(dat, bak)
    shutil.copy2(src, dat)
    return dat


def can_snapshot(save_path):
    return _find_dat(save_path) is not None
