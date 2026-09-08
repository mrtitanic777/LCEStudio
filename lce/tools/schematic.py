"""Build transplant — export a box of a world as a portable MCEdit `.schematic`
(gzipped NBT) and stamp schematics back into any (writable, old-NBT) world.

The `.schematic` format is the classic MCEdit/WorldEdit one, so builds exported here
open in those tools and vice-versa. Blocks are the standard YZX byte order
(index = (y*Length + z)*Width + x); block ids <=255 map 1:1 to LCE ids.

    from lce import schematic as S
    b, d = S.extract(world, x0,y0,z0, x1,y1,z1)   # world = a view3d World
    S.save(b, d, "titanic.schematic", name="Titanic")
    b, d, tes = S.load("titanic.schematic")       # tes = chests/signs (relative coords)
    n = S.stamp(world, b, d, ox, oy, oz)          # returns blocks written (+ dirty chunks)
"""
import gzip

import numpy as np

from .. import nbt as N
from ..view3d.world import CHUNK_X, CHUNK_Y


def _copy_compound(c):
    """Deep copy of an NBT compound (via re-serialize/parse) so we never mutate the
    source world's tile-entities."""
    _n, tag, _ = N.parse_tag(N.serialize("", c, N.COMPOUND))
    return tag.value


def save(blocks, data, path, name="build", tile_entities=None):
    """Write blocks/data (np.uint8 [W,H,L] in x,y,z) as a gzipped .schematic.
    tile_entities: optional list of NBT compounds (chests/signs/…) with x,y,z RELATIVE
    to the schematic origin (0,0,0), so they travel with the build."""
    W, H, L = blocks.shape
    flat_b = np.ascontiguousarray(blocks.transpose(1, 2, 0)).reshape(-1)   # -> YZX
    flat_d = np.ascontiguousarray(data.transpose(1, 2, 0)).reshape(-1)
    root = N.Compound()
    root.set("Width", N.SHORT, int(W))
    root.set("Height", N.SHORT, int(H))
    root.set("Length", N.SHORT, int(L))
    root.set("Materials", N.STRING, "Alpha")
    root.set("Blocks", N.BYTE_ARRAY, flat_b.tobytes())
    root.set("Data", N.BYTE_ARRAY, flat_d.tobytes())
    root.set("Entities", N.LIST, N.List(N.COMPOUND, []))
    root.set("TileEntities", N.LIST, N.List(N.COMPOUND, list(tile_entities or [])))
    root.set("Name", N.STRING, name)
    with gzip.open(path, "wb") as f:
        f.write(N.serialize("Schematic", root, N.COMPOUND))
    return W, H, L


def load(path):
    """Read a .schematic -> (blocks, data, tile_entities) — blocks/data np.uint8 [W,H,L]
    in x,y,z; tile_entities a list of NBT compounds with RELATIVE x,y,z."""
    raw = open(path, "rb").read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    _n, tag, _ = N.parse_tag(raw)
    c = tag.value
    W, H, L = int(c.get_value("Width")), int(c.get_value("Height")), int(c.get_value("Length"))

    def _u8(v):                                   # BYTE_ARRAY may parse as bytes or list
        if isinstance(v, (bytes, bytearray)):
            return np.frombuffer(bytes(v), np.uint8)
        return np.array(v, np.uint8)
    b = _u8(c.get_value("Blocks"))
    dv = c.get_value("Data")
    d = _u8(dv) if dv is not None else np.zeros(W * H * L, np.uint8)
    blocks = np.ascontiguousarray(b[:W * H * L].reshape(H, L, W).transpose(2, 0, 1))  # -> x,y,z
    data = np.ascontiguousarray(d[:W * H * L].reshape(H, L, W).transpose(2, 0, 1))
    te = c.get_value("TileEntities")
    tes = list(te.items) if te is not None and hasattr(te, "items") else []
    return blocks, data, tes


def extract_tile_entities(lworld, x0, y0, z0, x1, y1, z1):
    """Collect the tile-entities (chests/signs/furnaces/…) inside a box of an lce.World,
    returned as compounds with x,y,z made RELATIVE to (min corner). Deep-copied."""
    x0, x1 = sorted((int(x0), int(x1))); y0, y1 = sorted((int(y0), int(y1)))
    z0, z1 = sorted((int(z0), int(z1)))
    out = []
    for cx in range(x0 // CHUNK_X, x1 // CHUNK_X + 1):
        for cz in range(z0 // CHUNK_X, z1 // CHUNK_X + 1):
            ch = lworld.chunk(cx, cz)
            tes = ch.tile_entities if ch is not None else None
            if not (tes and tes.items):
                continue
            for te in tes.items:
                tx, ty, tz = te.get_value("x"), te.get_value("y"), te.get_value("z")
                if tx is None or not (x0 <= tx <= x1 and y0 <= ty <= y1 and z0 <= tz <= z1):
                    continue
                c = _copy_compound(te)
                c.set("x", N.INT, tx - x0); c.set("y", N.INT, ty - y0); c.set("z", N.INT, tz - z0)
                out.append(c)
    return out


def stamp_tile_entities(lworld, tes, ox, oy, oz):
    """Write schematic tile-entities (RELATIVE x,y,z) into an lce.World at origin
    (ox,oy,oz): offsets them to world coords, replaces any existing TE at each spot.
    Returns the count written."""
    n = 0
    for te in tes:
        rx, ry, rz = te.get_value("x"), te.get_value("y"), te.get_value("z")
        if rx is None:
            continue
        wx, wy, wz = ox + rx, oy + ry, oz + rz
        ch = lworld.chunk(wx >> 4, wz >> 4)
        if ch is None or not (0 <= wy < CHUNK_Y):
            continue
        c = _copy_compound(te)
        c.set("x", N.INT, wx); c.set("y", N.INT, wy); c.set("z", N.INT, wz)
        lst = ch.tile_entities
        if lst is None:
            ch.add_tile_entity(c)
        else:
            lst.etype = N.COMPOUND                     # an empty list can carry a stale etype
            lst.items[:] = [t for t in lst.items
                            if (t.get_value("x"), t.get_value("y"), t.get_value("z")) != (wx, wy, wz)]
            lst.items.append(c)
        ch._mark()
        if hasattr(ch, "_te_pos_cache"):
            ch._te_pos_cache = None
        n += 1
    return n


def extract(world, x0, y0, z0, x1, y1, z1):
    """Cut a box out of a view3d World -> blocks/data [W,H,L] (x,y,z). Handles any
    source format (the world is already decoded to arrays)."""
    x0, x1 = sorted((int(x0), int(x1)))
    y0, y1 = sorted((int(y0), int(y1)))
    z0, z1 = sorted((int(z0), int(z1)))
    y0 = max(0, y0); y1 = min(CHUNK_Y - 1, y1)
    W, H, L = x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1
    blocks = np.zeros((W, H, L), np.uint8)
    data = np.zeros((W, H, L), np.uint8)
    cx0, cx1 = x0 // CHUNK_X, x1 // CHUNK_X
    cz0, cz1 = z0 // CHUNK_X, z1 // CHUNK_X
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            arr = world.chunks.get((cx, cz))
            if arr is None:
                continue
            wx0, wx1 = max(x0, cx * CHUNK_X), min(x1, cx * CHUNK_X + 15)
            wz0, wz1 = max(z0, cz * CHUNK_X), min(z1, cz * CHUNK_X + 15)
            lx0, lz0 = wx0 - cx * CHUNK_X, wz0 - cz * CHUNK_X
            sub = arr[lx0:lx0 + (wx1 - wx0 + 1), lz0:lz0 + (wz1 - wz0 + 1), y0:y1 + 1]
            bx, bz = wx0 - x0, wz0 - z0
            blocks[bx:bx + sub.shape[0], :, bz:bz + sub.shape[1]] = sub.transpose(0, 2, 1)
            dd = world.data.get((cx, cz))
            if dd is not None:
                subd = dd[lx0:lx0 + (wx1 - wx0 + 1), lz0:lz0 + (wz1 - wz0 + 1), y0:y1 + 1]
                data[bx:bx + subd.shape[0], :, bz:bz + subd.shape[1]] = subd.transpose(0, 2, 1)
    return blocks, data


# "natural" blocks the magic wand treats as terrain (it never crosses these); a
# structure = the connected run of everything else. Deliberately does NOT include logs
# (17), leaves (18), cactus (81) or sandstone (24) -- those are common BUILD materials
# (wooden ships, sandstone pyramids), and treating them as terrain was exactly what
# clipped big builds like the Titanic mid-hull. Only true ground/water/ore/plants stop
# the fill.
_NATURAL = frozenset({
    1, 2, 3, 7, 8, 9, 10, 11, 12, 13,                            # stone/grass/dirt/bedrock/water/lava/sand/gravel
    14, 15, 16, 21, 56, 73, 74, 129,                             # ores
    79, 80, 78, 82, 87, 88, 110,                                 # ice/snow/clay/netherrack/soulsand/mycelium
    6, 31, 32, 37, 38, 39, 40, 83, 111, 106,                     # saplings / plants / vines / lily
})


# Face-adjacent (6-connectivity) neighbour offsets -- the default. Diagonal (26-conn)
# bridging is what let a flood LEAK off a ship's hull through a single corner-touching
# block (kelp/coral/rigging) into the whole surrounding ocean, ballooning the box far
# bigger than the build. Face-adjacency keeps the fill on the actual connected structure.
_FACE6 = ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1))
_DIAG26 = tuple((dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for dz in (-1, 0, 1) if dx or dy or dz)


def select_structure(world, x, y, z, max_blocks=40_000_000, natural=_NATURAL,
                     diagonal=False, bounds=None):
    """Flood-fill the built structure containing (x,y,z): the connected run of non-air,
    non-`natural` blocks. Returns a set of (x,y,z) coords, or None if the start block is
    air/terrain. The cap is effectively unbounded (40M) so a whole ship/castle is
    captured, not clipped.

    `diagonal=False` (default) uses FACE adjacency (6-conn): the fill only crosses shared
    block faces, so it can't leap a diagonal gap off the hull into the surrounding water/
    terrain. `diagonal=True` restores 26-connectivity for builds whose parts only touch
    corner-to-corner.

    `bounds=(x0,y0,z0,x1,y1,z1)` (inclusive) clamps the flood to that box, so a build that
    is physically JOINED to something else (a ship in a dry dock, a house on a wall) can be
    isolated: the fill captures only the connected blocks that lie inside the box and never
    escapes past it. Without bounds the fill follows the whole connected structure."""
    start = world.block(x, y, z)
    if start == 0 or start in natural:
        return None
    if bounds is not None:
        bx0, bx1 = sorted((int(bounds[0]), int(bounds[3])))
        by0, by1 = sorted((int(bounds[1]), int(bounds[4])))
        bz0, bz1 = sorted((int(bounds[2]), int(bounds[5])))

        def in_box(px, py, pz):
            return bx0 <= px <= bx1 and by0 <= py <= by1 and bz0 <= pz <= bz1
    else:
        def in_box(px, py, pz):
            return True
    if not in_box(int(x), int(y), int(z)):
        return None
    nbrs = _DIAG26 if diagonal else _FACE6
    seen = set()
    stack = [(int(x), int(y), int(z))]
    while stack and len(seen) < max_blocks:
        p = stack.pop()
        if p in seen:
            continue
        bx, by, bz = p
        if not (0 <= by < CHUNK_Y) or not in_box(bx, by, bz):
            continue
        b = world.block(bx, by, bz)
        if b == 0 or b in natural:
            continue
        seen.add(p)
        for dx, dy, dz in nbrs:
            q = (bx + dx, by + dy, bz + dz)
            if q not in seen:
                stack.append(q)
    return seen


def cells_to_arrays(world, cells):
    """A set of coords -> (blocks, data [W,H,L], (minx,miny,minz)); voxels outside
    the structure are air, so it stamps as a clean overlay."""
    xs = [c[0] for c in cells]; ys = [c[1] for c in cells]; zs = [c[2] for c in cells]
    x0, y0, z0 = min(xs), min(ys), min(zs)
    W, H, L = max(xs) - x0 + 1, max(ys) - y0 + 1, max(zs) - z0 + 1
    blocks = np.zeros((W, H, L), np.uint8)
    data = np.zeros((W, H, L), np.uint8)
    for (x, y, z) in cells:
        cx, lx = divmod(x, CHUNK_X); cz, lz = divmod(z, CHUNK_X)
        arr = world.chunks.get((cx, cz))
        if arr is None:
            continue
        blocks[x - x0, y - y0, z - z0] = arr[lx, lz, y]
        dd = world.data.get((cx, cz))
        if dd is not None:
            data[x - x0, y - y0, z - z0] = dd[lx, lz, y]
    return blocks, data, (x0, y0, z0)


def stamp(world, blocks, data, ox, oy, oz, skip_air=True, height=CHUNK_Y):
    """Write a schematic into a World at origin (ox,oy,oz). Returns (n_written,
    dirty_chunks). skip_air leaves existing blocks where the schematic is air.
    Ground check: the origin is lowered so the whole build fits under the world's
    `height` ceiling (and not below y=0) — so nothing gets clipped when it fits at
    all. `height` MUST match the target world: 256 for a view3d World, 128 for the
    128-tall old-NBT `lce.World` (writing y>=128 there corrupts lower blocks or
    raises IndexError), so callers of the latter must pass height=128."""
    W, H, L = blocks.shape
    if oy + H > height:                      # would poke through the ceiling -> drop it down
        oy = height - H
    oy = max(0, oy)                          # (if H>height it can't fully fit; bottom-aligned)
    n = 0
    dirty = set()
    for xi in range(W):
        for yi in range(H):
            y = oy + yi
            if not (0 <= y < height):
                continue
            for zi in range(L):
                bid = int(blocks[xi, yi, zi])
                if skip_air and bid == 0:
                    continue
                try:
                    ch = world.set_block(ox + xi, y, oz + zi, bid, int(data[xi, yi, zi]))
                except IndexError:           # y past this world's real ceiling -> skip, never crash
                    ch = None
                if ch is not None:
                    dirty.add(ch); n += 1
    return n, dirty
