"""Terrain generators for an explored LCE world.

  * flatten(world)      — strip everything above the water line, leaving a flat
                          world at sea level (oceans stay water, land stays land).
                          Optionally keep player builds standing.
  * add_mountains(world)— raise value-noise mountains back into a (flat) world:
                          stone bulk, dirt+grass cap, snow on the high peaks.

Both operate on `lce.World` (the writable engine) and rebuild chunks in bulk (a
whole-world edit is far too big for the per-block edit path). Writable = old-NBT
worlds; Aquatic is read-only across the toolkit.
"""
import numpy as np
from PIL import Image

from .schematic import _NATURAL

SEA_Y = 62                                     # water surface (y0..62 = at/under water)
_STONE, _DIRT, _GRASS, _SNOW = 1, 3, 2, 80


def _natural_mask():
    m = np.zeros(256, bool)
    for i in _NATURAL:
        m[i] = True
    return m


def _regions(world):
    for name in [n for n in world._filedata if n.endswith(".mcr") and not n.startswith("DIM")]:
        rx, rz = (int(v) for v in name[:-4].split("/")[-1].split(".")[1:3])
        reg = world.region(rx, rz)
        reg.decode_all()                    # worldgen scans every chunk
        yield rx, rz, reg


def flatten(world, sea_y=SEA_Y, keep_builds=True, grass_cap=True, log=print):
    """Cut terrain above `sea_y` down to a flat world. keep_builds leaves placed
    (non-natural) blocks standing; grass_cap turns the exposed stone/dirt land into
    a clean grass surface (sand/gravel beaches and water are left as-is). Returns
    (chunks_changed, blocks_cleared)."""
    nat = _natural_mask()
    n_chunks = cleared = 0
    for rx, rz, reg in _regions(world):
        for (lcx, lcz), ch in reg.chunks.items():
            bl = ch._blocks()
            if len(bl) != 32768:
                continue
            arr = np.array(bl, np.uint8).reshape(16, 16, 128)      # [x,z,y]
            above = arr[:, :, sea_y + 1:]
            mask = nat[above] if keep_builds else (above != 0)
            c = int(mask.sum())
            changed = c > 0
            if c:
                above[mask] = 0
            if grass_cap:                                          # cap exposed land with grass
                surf = arr[:, :, sea_y]
                cap = (surf == _STONE) | (surf == _DIRT)
                if cap.any():
                    surf[cap] = _GRASS
                    changed = True
            if changed:
                bl[:] = arr.reshape(-1).tolist()
                ch._mark(); n_chunks += 1; cleared += c
        log("flatten: region %d,%d" % (rx, rz))
    return n_chunks, cleared


def _value_noise(W, L, scale, octaves, rng):
    """Multi-octave value noise in [0,1], shape (W, L), via bilinear-upsampled
    random grids (PIL does the upsample)."""
    hm = np.zeros((W, L), np.float64)
    amp = 1.0; total = 0.0
    for o in range(octaves):
        s = max(2, scale >> o)
        gw, gl = max(2, W // s + 2), max(2, L // s + 2)
        grid = (rng.rand(gw, gl) * 255).astype(np.uint8)
        up = np.asarray(Image.fromarray(grid).resize((L, W), Image.BILINEAR), np.float64) / 255.0
        hm += up * amp; total += amp; amp *= 0.5
    return hm / total


def add_mountains(world, max_height=45, scale=40, coverage=0.55, roughness=3,
                  sea_y=SEA_Y, snow_y=98, seed=1337, log=print):
    """Raise value-noise mountains above `sea_y`. `coverage` 0..1 = fraction of the
    map that stays low (higher -> sparser peaks); `max_height` = tallest peak above
    sea. Columns build stone up to the target, a dirt+grass cap, snow above snow_y.
    Returns (chunks_changed, blocks_added)."""
    chunks = list(_regions(world))
    keys = [(rx, rz, lcx, lcz) for rx, rz, reg in chunks for (lcx, lcz) in reg.chunks]
    if not keys:
        return 0, 0
    gxs = [rx * 512 + lcx * 16 for rx, rz, lcx, lcz in keys]
    gzs = [rz * 512 + lcz * 16 for rx, rz, lcx, lcz in keys]
    minx, minz = min(gxs), min(gzs)
    W = (max(gxs) - minx) + 16
    L = (max(gzs) - minz) + 16
    rng = np.random.RandomState(seed)
    noise = _value_noise(W, L, scale, roughness, rng)          # (W, L) over x,z
    thr = coverage
    peak = np.clip((noise - thr) / (1.0 - thr), 0.0, 1.0) ** 1.5
    height = (peak * max_height).astype(np.int32)              # per-column rise above sea

    n_chunks = added = 0
    for rx, rz, reg in _regions(world):
        for (lcx, lcz), ch in reg.chunks.items():
            bl = ch._blocks()
            if len(bl) != 32768:
                continue
            arr = np.array(bl, np.uint8).reshape(16, 16, 128)
            base_x = rx * 512 + lcx * 16 - minx
            base_z = rz * 512 + lcz * 16 - minz
            touched = False
            for lx in range(16):
                for lz in range(16):
                    h = int(height[base_x + lx, base_z + lz])
                    if h <= 0:
                        continue
                    top = min(127, sea_y + h)
                    col = arr[lx, lz]
                    col[sea_y + 1:top + 1] = _STONE
                    if top >= snow_y:
                        col[top] = _SNOW
                    else:
                        col[top] = _GRASS
                        if top - 1 > sea_y:
                            col[top - 1] = _DIRT
                    added += top - sea_y; touched = True
            if touched:
                bl[:] = arr.reshape(-1).tolist()
                ch._mark(); n_chunks += 1
        log("mountains: region %d,%d" % (rx, rz))
    return n_chunks, added


# ---------------------------------------------------------------- challenge worlds
import re as _re
from . import nbt as _N

# a modest survival starter kit (TU0 numeric item ids)
_STARTER = [(6, 4, 0), (295, 8, 0), (338, 2, 0), (338, 0x2, 0), (352, 3, 0)]
# classic Skyblock chest
_SKYBLOCK_CHEST = [
    (327, 1, 0),   # lava bucket
    (79, 2, 0),    # ice x2
    (6, 2, 0),     # saplings
    (81, 1, 0),    # cactus
    (338, 1, 0),   # sugar cane
    (295, 1, 0),   # seeds
    (39, 1, 0), (40, 1, 0),   # brown / red mushroom
    (361, 1, 0), (362, 1, 0), # pumpkin / melon seeds
    (352, 3, 0),   # bone x3
    (325, 1, 0),   # bucket
]


def _extent(world):
    xs, zs = [], []
    for name in world._filedata:
        m = _re.match(r"r\.(-?\d+)\.(-?\d+)\.mcr$", name)
        if m:
            rx, rz = int(m.group(1)), int(m.group(2))
            xs += [rx * 32 * 16, (rx * 32 + 31) * 16 + 15]
            zs += [rz * 32 * 16, (rz * 32 + 31) * 16 + 15]
    if not xs:
        return None
    return min(xs), min(zs), max(xs), max(zs)


def _tree(world, x, y, z):
    for i in range(4):
        world.set_block(x, y + i, z, 17, 0)                 # oak log
    for dy in (3, 4):
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                if abs(dx) == 2 and abs(dz) == 2:
                    continue
                if dx == 0 and dz == 0 and dy == 3:
                    continue
                world.set_block(x + dx, y + dy, z + dz, 18, 0)   # leaves
    world.set_block(x, y + 4, z, 18, 0)
    world.set_block(x, y + 5, z, 18, 0)


def _island(world, cx, cy, cz, r=7):
    for dx in range(-r, r + 1):
        for dz in range(-r, r + 1):
            d2 = dx * dx + dz * dz
            if d2 <= r * r:
                depth = 3 if d2 <= (r - 2) ** 2 else 1
                for k in range(1, depth + 1):
                    world.set_block(cx + dx, cy - k, cz + dz, 3, 0)   # dirt
                world.set_block(cx + dx, cy, cz + dz, 2, 0)           # grass


def _add_chest(world, x, y, z, items):
    world.set_block(x, y, z, 54, 0)                         # chest block
    ch = world.chunk(x >> 4, z >> 4)
    if ch is None:
        return
    te = _N.Compound()
    te.set("id", _N.STRING, "Chest")
    te.set("x", _N.INT, x); te.set("y", _N.INT, y); te.set("z", _N.INT, z)
    lst = _N.List(_N.COMPOUND, [])
    for slot, (iid, cnt, dmg) in enumerate(items):
        it = _N.Compound()
        it.set("id", _N.SHORT, iid); it.set("Count", _N.BYTE, cnt)
        it.set("Damage", _N.SHORT, dmg); it.set("Slot", _N.BYTE, slot)
        lst.items.append(it)
    te.set("Items", _N.LIST, lst)
    ch.add_tile_entity(te)


CHALLENGES = ("skyblock", "one-chunk", "void", "island")


def generate_challenge(world, kind="skyblock", log=print):
    """Wipe an (old-NBT) world to the void and build a ready-to-play challenge start:
    skyblock, one-chunk, void, or a survival island. Sets the spawn, moves every player
    onto it in survival with an empty inventory, and stocks a starter chest. Operates in
    place — run it on a COPY (or save to a new folder)."""
    kind = kind.lower().replace(" ", "-")
    if kind not in CHALLENGES:
        raise ValueError("unknown challenge %r (use: %s)" % (kind, ", ".join(CHALLENGES)))
    ext = _extent(world)
    if ext:
        log("clearing world to the void…")
        world.fill(ext[0], 0, ext[1], ext[2], 127, ext[3], 0)      # void everything
    sx, sy, sz = 0, 65, 0
    if kind == "void":
        world.fill(-1, 64, -1, 1, 64, 1, 20)                       # glass platform
    elif kind == "one-chunk":
        world.fill(0, 0, 0, 15, 0, 15, 7)                          # bedrock
        world.fill(0, 1, 0, 15, 59, 15, 1)                         # stone
        world.fill(0, 60, 0, 15, 62, 15, 3)                        # dirt
        world.fill(0, 63, 0, 15, 63, 15, 2)                        # grass
        _tree(world, 4, 64, 4)
        _add_chest(world, 8, 64, 8, _STARTER)
        sx, sy, sz = 8, 64, 8
    elif kind == "island":
        _island(world, 0, 63, 0, r=7)
        for tx, tz in ((-3, -3), (3, 2), (-2, 4)):
            _tree(world, tx, 64, tz)
        _add_chest(world, 1, 64, 0, _STARTER)
        sx, sy, sz = 0, 64, 0
    else:  # skyblock
        world.fill(-1, 62, -1, 1, 63, 1, 3)                        # dirt cube
        world.fill(-1, 64, -1, 1, 64, 1, 2)                        # grass top
        _tree(world, -1, 65, -1)
        _add_chest(world, 1, 65, 1, _SKYBLOCK_CHEST)
        world.fill(6, 64, 0, 8, 64, 2, 12)                         # sand side-island
        world.set_block(7, 65, 1, 81, 0)                           # cactus
        sx, sy, sz = 0, 65, 0
    log("placed %s at spawn (%d,%d,%d)" % (kind, sx, sy, sz))
    world.set_spawn(sx, sy, sz)
    world.set_level("LevelName", "%s Challenge" % kind.replace("-", " ").title())
    n = 0
    for pkey in (world.players() or []):
        world.edit_player(pkey, pos=(sx + 0.5, float(sy), sz + 0.5), spawn=(sx, sy, sz),
                          gametype=0, health=20, food=20, clear_inventory=True)
        n += 1
    log("moved %d player(s) to spawn in survival" % n)
    return {"kind": kind, "spawn": (sx, sy, sz), "players": n}
