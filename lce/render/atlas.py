"""World atlas renderer — a top-down surface map of an entire LCE world, plus a
renderer for the in-game map items (data/map_*.dat).

Colors come straight from the terrain atlas (each block's top-face tile, averaged
over its opaque texels, times the block's biome tint) so every block renders in a
faithful colour with full id coverage — no hand-maintained palette. Relief comes
from a north-neighbour hillshade (a column higher than the one to its north is lit,
lower is shadowed), the same trick Minecraft's own maps use.

Data source is the view3d `World` loader, so EVERY chunk format loads (legacy NBT
128/256-tall, compressed-storage v8/9/10, and Aquatic 0x0C) — unlike the engine's
region reader, which can't parse Aquatic.

    from lce import atlas
    img = atlas.render_world(atlas.load(save_path))        # PIL.Image
    img.save("world.png")
"""
import os
import struct

import numpy as np
from PIL import Image

from ..view3d import mesher
from ..view3d.world import World, CHUNK_X, CHUNK_Z, CHUNK_Y

_ATLAS_PATH = os.path.join(os.path.dirname(mesher.__file__), "textures", "terrain.png")  # anchored on the view3d pkg, move-proof
_UNGEN = (28, 31, 38)                          # un-generated columns
_WATER_IDS = (8, 9)
_color_cache = None


def load(save_path):
    """Load a world (any format) for rendering."""
    return World.load(save_path)


def block_color_table():
    """[256,3] uint8 — each block id's map colour, from its top-face atlas tile
    (mean of opaque texels) times its tint. Cached after first build."""
    global _color_cache
    if _color_cache is not None:
        return _color_cache
    a = np.asarray(Image.open(_ATLAS_PATH).convert("RGBA"))     # [256,256,4]
    out = np.zeros((256, 3), np.float64)
    for bid in range(256):
        t = int(mesher._TILE_LUT[bid, 0, 0])                    # top-face tile index
        col, row = t % 16, t // 16
        patch = a[row * 16:(row + 1) * 16, col * 16:(col + 1) * 16]
        mask = patch[:, :, 3] > 128
        rgb = patch[:, :, :3][mask].mean(axis=0) if mask.any() else np.zeros(3)
        tint = mesher._TINT[bid, 0]                             # biome/leaf/grass tint
        out[bid] = np.clip(rgb * tint, 0, 255)
    _color_cache = out.astype(np.uint8)
    return _color_cache


def render_world(world, shade=True, water_depth=True):
    """Top-down surface map of the whole world -> PIL.Image (1 px per block, north
    up / east right). Un-generated area is dark; each generated column shows its
    top non-air block, hillshaded."""
    if not world.chunks:
        return Image.new("RGB", (16, 16), _UNGEN)
    colors = block_color_table()
    xs = [c[0] for c in world.chunks]; zs = [c[1] for c in world.chunks]
    mincx, maxcx, mincz, maxcz = min(xs), max(xs), min(zs), max(zs)
    W = (maxcx - mincx + 1) * CHUNK_X
    H = (maxcz - mincz + 1) * CHUNK_Z
    top_id = np.zeros((H, W), np.uint8)          # row = z (north at top), col = x
    top_h = np.full((H, W), -1, np.int16)
    ii, jj = np.indices((CHUNK_X, CHUNK_Z))
    for (cx, cz), arr in world.chunks.items():
        nz = arr != 0                            # [x,z,y]
        any_s = nz.any(axis=2)
        ty = (CHUNK_Y - 1) - np.argmax(nz[:, :, ::-1], axis=2)
        tid = arr[ii, jj, ty]
        tid = np.where(any_s, tid, 0).astype(np.uint8)
        ty = np.where(any_s, ty, -1).astype(np.int16)
        gx0 = (cx - mincx) * CHUNK_X; gz0 = (cz - mincz) * CHUNK_Z
        top_id[gz0:gz0 + CHUNK_Z, gx0:gx0 + CHUNK_X] = tid.T    # arr[x,z] -> image[z,x]
        top_h[gz0:gz0 + CHUNK_Z, gx0:gx0 + CHUNK_X] = ty.T

    img = colors[top_id].astype(np.float32)
    if shade:
        dh = np.zeros((H, W), np.int16)
        dh[1:, :] = top_h[1:, :] - top_h[:-1, :]               # vs the column to the north
        f = np.ones((H, W), np.float32)
        f[dh > 0] = 1.14
        f[dh < 0] = 0.78
        img *= f[:, :, None]
    if water_depth:                                            # deeper water = darker blue
        wmask = np.isin(top_id, _WATER_IDS)
        if wmask.any():
            depth = np.clip((62 - top_h), 0, 30) / 30.0
            img[wmask] *= (1.0 - 0.45 * depth[wmask])[:, None]
    img = np.clip(img, 0, 255).astype(np.uint8)
    img[top_h < 0] = _UNGEN
    return Image.fromarray(img, "RGB")


def world_origin(world):
    """Block-space (x0, z0) of the top-left of a render_world/render_slice image."""
    if not world.chunks:
        return 0, 0
    return min(c[0] for c in world.chunks) * CHUNK_X, min(c[1] for c in world.chunks) * CHUNK_Z


def render_slice(world, y, shade=False):
    """Horizontal cross-section at height `y` -> PIL.Image (format-complete)."""
    if not world.chunks:
        return Image.new("RGB", (16, 16), _UNGEN)
    y = max(0, min(CHUNK_Y - 1, int(y)))
    colors = block_color_table()
    xs = [c[0] for c in world.chunks]; zs = [c[1] for c in world.chunks]
    mincx, maxcx, mincz, maxcz = min(xs), max(xs), min(zs), max(zs)
    W = (maxcx - mincx + 1) * CHUNK_X
    H = (maxcz - mincz + 1) * CHUNK_Z
    top_id = np.zeros((H, W), np.uint8)
    present = np.zeros((H, W), bool)
    for (cx, cz), arr in world.chunks.items():
        sl = arr[:, :, y]                                    # [x,z]
        gx0 = (cx - mincx) * CHUNK_X; gz0 = (cz - mincz) * CHUNK_Z
        top_id[gz0:gz0 + CHUNK_Z, gx0:gx0 + CHUNK_X] = sl.T
        present[gz0:gz0 + CHUNK_Z, gx0:gx0 + CHUNK_X] = True
    img = colors[top_id]
    img[~present] = _UNGEN
    return Image.fromarray(img, "RGB")


# ------------------------------------------------------------ in-game map items
# Minecraft/LCE base map colours; final colour = base * shade (shade = index & 3).
_MAP_BASE = [
    (0, 0, 0), (127, 178, 56), (247, 233, 163), (199, 199, 199), (255, 0, 0),
    (160, 160, 255), (167, 167, 167), (0, 124, 0), (255, 255, 255), (164, 168, 184),
    (151, 109, 77), (112, 112, 112), (64, 64, 255), (143, 119, 72), (255, 252, 245),
    (216, 127, 51), (178, 76, 216), (102, 153, 216), (229, 229, 51), (127, 204, 25),
    (242, 127, 165), (76, 76, 76), (153, 153, 153), (76, 127, 153), (127, 63, 178),
    (51, 76, 178), (102, 76, 51), (102, 127, 51), (153, 51, 51), (25, 25, 25),
    (250, 238, 77), (92, 219, 213), (74, 128, 255), (0, 217, 58), (129, 86, 49),
    (112, 2, 0),
]
_MAP_SHADE = (180, 220, 255, 135)


def _map_palette():
    pal = np.zeros((len(_MAP_BASE) * 4, 3), np.float64)
    for i, base in enumerate(_MAP_BASE):
        for s in range(4):
            pal[i * 4 + s] = np.array(base) * _MAP_SHADE[s] / 255.0
    return np.clip(pal, 0, 255).astype(np.uint8)


def render_map_item(dat_bytes):
    """Render an in-game map (data/map_*.dat) -> a 128x128 PIL.Image, or None."""
    idx = dat_bytes.rfind(b"colors")
    if idx < 0:
        return None
    p = idx + 6
    n = struct.unpack_from(">i", dat_bytes, p)[0]; p += 4       # TAG_Byte_Array length
    if n < 128 * 128:
        return None
    raw = np.frombuffer(dat_bytes[p:p + 128 * 128], np.uint8).astype(np.int64)
    pal = _map_palette()
    raw = np.clip(raw, 0, len(pal) - 1)
    img = pal[raw].reshape(128, 128, 3).astype(np.uint8)
    return Image.fromarray(img, "RGB")
