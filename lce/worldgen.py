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
