"""World Library — a fast scan of one or more saves folders into gallery entries
(name + platform + thumbnail + title update), reusing the converter engine's cheap
metadata readers (no decompression).

``scan`` is fast: it takes a first-guess title update from the folder path in a
Nexia/emulator library (``.../NO_TU/...`` or ``.../TU19/...``). ``enrich`` is the
slower, accurate second pass — it reads each world's payload ONCE to (a) replace that
guess with the real, content-based title update (``accurate_tu``) and (b) fingerprint
the payload so byte-identical **duplicate** saves in different folders/containers are
grouped, even when their filenames differ.

    from lce import library as L
    worlds = L.scan(["C:/Nexia360/Library"])       # [{path,name,platform,thumbnail,tu,detail}]
    dup_groups = L.enrich(worlds)                  # accurate tu + fingerprint + dup_paths, in place
"""
import hashlib
import os
import re
from pathlib import Path

_TU_RE = re.compile(r"(?:^|[\\/])(NO_TU|TU\d+)(?:[\\/]|$)", re.I)

# how a platform code shows on a card
PLATFORM_LABEL = {"xbox360": "Xbox 360", "ps3": "PS3", "windows_lce": "Windows LCE"}

# platforms whose payload the engine can read big-endian for a content-based TU;
# Windows LCE (saveData.ms) isn't a console payload, so it stays path-based.
_CONTENT_ENDIAN = {"xbox360": ">", "ps3": ">"}


def tu_from_path(p):
    """The title update named by a Nexia/emulator library path, or None."""
    m = _TU_RE.search(str(p))
    if not m:
        return None
    t = m.group(1).upper()
    return "TU0" if t == "NO_TU" else t


def read_payload(path, platform):
    """The decompressed big-endian console payload for a world, or None. Reused for
    both accurate TU and the duplicate fingerprint so a world is read only once."""
    if platform not in _CONTENT_ENDIAN:
        return None
    try:
        from ..converter import lce_engine as E
        payload, _n, _t = E.read_console_input(path, platform)
        return payload
    except Exception:
        return None


def _norm_tu_text(txt):
    """Tidy the engine's TU text for display. It returns an INVERTED range like
    'TU69 - TU59' when its three bounds (save version, chunk version, level.dat)
    contradict — a hybrid/edited world — which reads as broken on a card, so order
    it and flag the uncertainty: 'TU59 - TU69?'."""
    nums = [int(n) for n in re.findall(r"TU(\d+)", txt or "")]
    if len(nums) == 2 and nums[0] > nums[1]:
        return "TU%d - TU%d?" % (nums[1], nums[0])
    return txt


def accurate_tu(path, platform, payload=None):
    """The real title update read from the SAVE CONTENT (e.g. 'TU31' or 'TU46 - TU68'),
    not the folder name. Falls back to the path-named TU, then None. `payload` may be
    passed to avoid re-reading the save."""
    endian = _CONTENT_ENDIAN.get(platform)
    if endian:
        if payload is None:
            payload = read_payload(path, platform)
        if payload is not None:
            try:
                from ..converter import lce_engine as E
                txt = E.title_update_text(payload, endian)
                if txt:
                    return _norm_tu_text(txt)
            except Exception:
                pass
    return tu_from_path(path)


def fingerprint(path, platform, payload=None):
    """A content fingerprint identifying the actual world, so byte-identical copies in
    different folders/containers hash the same. Uses the decompressed payload (ignores
    the STFS wrapper); falls back to a size+head hash of the file when the payload can't
    be read. Returns a hex string, or None."""
    if payload is None:
        payload = read_payload(path, platform)
    if payload is not None:
        return "p:" + hashlib.sha1(payload).hexdigest()
    try:                                                # fallback: cheap file signature
        p = Path(path)
        if p.is_dir():
            return None
        size = p.stat().st_size
        with open(p, "rb") as f:
            head = f.read(65536)
        return "f:%d:%s" % (size, hashlib.sha1(head).hexdigest())
    except Exception:
        return None


def enrich(worlds, log=None, on_world=None):
    """Second pass over `scan` results: read each world's payload ONCE to set an accurate
    content-based `tu` and a duplicate `fingerprint`, then group duplicates. Mutates each
    world dict in place, adding `fingerprint` and (for any world sharing its fingerprint
    with another) `dup_paths` — the OTHER copies' paths. Returns the list of duplicate
    groups (each a list of world dicts, 2+ long). `on_world(i, world)` is called after each
    world so a GUI can refine cards incrementally."""
    groups = {}
    for i, w in enumerate(worlds):
        payload = read_payload(w["path"], w["platform"])
        tu = accurate_tu(w["path"], w["platform"], payload=payload)
        if tu:
            w["tu"] = tu
        fp = fingerprint(w["path"], w["platform"], payload=payload)
        w["fingerprint"] = fp
        if fp:
            groups.setdefault(fp, []).append(w)
        if on_world:
            on_world(i, w)
        if log:
            log("checking %s… (%d/%d)" % (w.get("name", "?"), i + 1, len(worlds)))
    dup_groups = [g for g in groups.values() if len(g) > 1]
    for g in dup_groups:
        paths = [m["path"] for m in g]
        for m in g:
            m["dup_paths"] = [p for p in paths if p != m["path"]]
    return dup_groups


def _clean_name(name):
    n = str(name or "").strip()
    if n.lower().endswith(".bin"):
        n = n[:-4]
    return n or "world"


def scan(folders, log=None, max_depth=8):
    """Walk each folder (bounded depth) and return a de-duplicated, name-sorted list of
    world entries: {path, name, platform, thumbnail (PNG bytes|None), tu (str|None),
    detail}. Fast — only names/thumbnails are read, nothing is decompressed."""
    from ..converter import lce_engine as E
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
        from ..converter import lce_engine as E
        payload, _name, _thumb = E.read_console_input(path, platform)
        return E.describe_world(payload, ">", platform)   # console payloads are big-endian
    except Exception:
        return None
