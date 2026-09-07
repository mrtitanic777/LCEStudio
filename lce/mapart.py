"""In-game Map painter — turn any image into an LCE map item (data/map_*.dat).

A map item stores a 128x128 grid of palette indices in a ``colors`` byte array.
Each byte is ``baseColour * 4 + shade`` (shade 0..3 = x0.71/0.86/1.0/0.53), which
`atlas.render_map_item` decodes back to a picture. This module goes the other way:
resize+dither an image to the map palette, and build (or repaint) a map_*.dat.

    from lce import mapart
    dat = mapart.image_to_map_dat(Image.open("logo.png"))    # bytes
    name = mapart.add_map(world, dat)                         # -> "data/map_3.dat"
    world.save(...)                                           # persist it
"""
import re
import struct

import numpy as np

from .atlas import _map_palette

MAP_W = MAP_H = 128
_COLORS_LEN = MAP_W * MAP_H          # 16384

# Palette indices 0..3 are base colour 0 = "unexplored / transparent" in-game, so a
# painted picture must not use them; quantise against bases 1+ only (147-3 = 144 colours).
_VOID = 4


def paint_palette():
    """[144,3] uint8 — the non-transparent map colours, in map-byte order (index+4)."""
    return _map_palette()[_VOID:]


def image_to_colors(pil_image, dither=True):
    """Resize an image to 128x128 and quantise it to the map palette. Returns a bytes
    object of 16384 palette indices (each already offset past the transparent range)."""
    from PIL import Image
    img = pil_image.convert("RGB").resize((MAP_W, MAP_H), Image.LANCZOS)
    pal = paint_palette()                                # [144,3]
    pimg = Image.new("P", (1, 1))
    flat = []
    for c in pal:
        flat += [int(c[0]), int(c[1]), int(c[2])]
    flat += [0] * (768 - len(flat))                      # PIL wants 256*3 entries
    pimg.putpalette(flat)
    q = img.quantize(palette=pimg,
                     dither=Image.FLOYDSTEINBERG if dither else Image.NONE)
    idx = np.asarray(q, np.uint8).reshape(MAP_W * MAP_H)
    idx = np.clip(idx, 0, len(pal) - 1) + _VOID          # -> real map byte
    return idx.astype(np.uint8).tobytes()


def _tag(tid, name):
    nb = name.encode("utf-8")
    return bytes([tid]) + struct.pack(">H", len(nb)) + nb


def build_map_dat(colors, dimension=0, scale=0, xcenter=0, zcenter=0):
    """A complete map_*.dat NBT (big-endian) wrapping a 16384-byte colour array."""
    colors = bytes(colors)
    if len(colors) != _COLORS_LEN:
        raise ValueError("colors must be %d bytes, got %d" % (_COLORS_LEN, len(colors)))
    d = bytearray()
    d += _tag(0x0A, "")                                          # root compound
    d += _tag(0x0A, "data")                                     # data compound
    d += _tag(0x01, "dimension") + bytes([dimension & 0xFF])
    d += _tag(0x03, "xCenter") + struct.pack(">i", int(xcenter))
    d += _tag(0x03, "zCenter") + struct.pack(">i", int(zcenter))
    d += _tag(0x01, "scale") + bytes([scale & 0xFF])
    d += _tag(0x02, "width") + struct.pack(">h", MAP_W)
    d += _tag(0x02, "height") + struct.pack(">h", MAP_H)
    d += _tag(0x01, "trackingPosition") + bytes([0])
    d += _tag(0x07, "colors") + struct.pack(">i", len(colors)) + colors
    d += b"\x00"                                                # end data
    d += b"\x00"                                                # end root
    return bytes(d)


def replace_colors(dat_bytes, colors):
    """Swap the colour array inside an EXISTING map_*.dat, keeping the rest of its NBT
    (dimension/scale/centre/banners/frames) byte-for-byte — the safest repaint."""
    colors = bytes(colors)
    if len(colors) != _COLORS_LEN:
        raise ValueError("colors must be %d bytes" % _COLORS_LEN)
    i = dat_bytes.rfind(b"colors")
    if i < 0:
        raise ValueError("no 'colors' array in this map .dat")
    p = i + 6
    n = struct.unpack_from(">i", dat_bytes, p)[0]
    return dat_bytes[:p + 4] + colors + dat_bytes[p + 4 + n:]


def image_to_map_dat(pil_image, dither=True, existing=None, scale=0):
    """Image -> map .dat. If `existing` (a map_*.dat's bytes) is given, its NBT skeleton
    is reused and only the picture changes; otherwise a fresh map is built."""
    colors = image_to_colors(pil_image, dither=dither)
    if existing:
        return replace_colors(existing, colors)
    return build_map_dat(colors, scale=scale)


# ---------------------------------------------------------------- world VFS helpers
_MAP_RE = re.compile(r"^data/map_(\d+)\.dat$", re.I)


def list_maps(world):
    """[(name, dat_bytes)] for every data/map_*.dat in the save, sorted by number."""
    out = []
    for name, blob in world._filedata.items():
        if _MAP_RE.match(name):
            out.append((name, blob))
    out.sort(key=lambda t: int(_MAP_RE.match(t[0]).group(1)))
    return out


def next_map_name(world):
    used = {int(_MAP_RE.match(n).group(1)) for n in world._filedata if _MAP_RE.match(n)}
    i = 0
    while i in used:
        i += 1
    return "data/map_%d.dat" % i


def add_map(world, dat_bytes, name=None):
    """Add a NEW map file to the save's VFS; returns its name. Persisted on world.save()."""
    name = name or next_map_name(world)
    ts = 0
    try:
        ts = max((e[3] for e in world._ents), default=0)
    except Exception:
        ts = 0
    world._filedata[name] = bytes(dat_bytes)
    if not any((e[0] == name) for e in world._ents):
        world._ents.append([name, 0, 0, ts])
    return name


def put_map(world, name, dat_bytes):
    """Replace an existing map file's bytes in the VFS (keeps its file-table entry)."""
    world._filedata[name] = bytes(dat_bytes)
    return name
