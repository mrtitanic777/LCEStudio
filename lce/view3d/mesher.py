"""Chunk block volume -> textured triangle mesh, using Minecraft Beta geometry.

Full cubes are face-culled (incl. same-type culling for glass/leaves/water/slab,
per Block.shouldSideBeRendered). Non-cube blocks get real shapes: boxes (slabs,
stairs, fences, cactus, snow, ...), crosses (plants), crops, torches, ladders,
rails. Every primitive is a 4-vertex quad. Vectorised where it matters."""
import numpy as np

from . import blocks
from .world import CHUNK_X, CHUNK_Z, CHUNK_Y

_OPAQUE = np.array(blocks.SOLID, dtype=bool)
_RENDER = np.array(blocks.RENDER, dtype=bool)
_CAT = np.array(blocks.SHAPE_CAT, dtype=np.int32)
_GRP = np.array(blocks.CULL_GROUP, dtype=np.int32)
_TILES = np.array(blocks.TILES, dtype=np.int32)
_TILE_LUT = np.array(blocks.TILE_LUT, dtype=np.int32)   # [id, meta, kind] -> tile
_TINT = np.array(blocks.TINT, dtype=np.float32)
_CUBE, _BOX, _CROSS, _CROPS, _TORCH, _LADDER, _RAIL = range(7)
_STAIR_IDS = set(blocks.STAIR_IDS)

_ATLAS = 16
_TSZE = 1.0 / _ATLAS
# Pull the UV in by a tiny fraction of a texel — just enough to stop a fragment
# sampling across the tile seam into the next tile (bleed), but NOT the old
# half-texel inset, which visibly shrank each tile's edge texels and made every
# texture look offset from the block-face edges under GL_NEAREST.
_INSET = 0.1 / (_ATLAS * 16)

# normal -> (corner 0/1 flags [4,3], corner uv [4,2]); CCW from outside
_FACES = {
    (1, 0, 0):  ([(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], [(0, 1), (0, 0), (1, 0), (1, 1)]),
    (-1, 0, 0): ([(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    (0, 1, 0):  ([(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], [(0, 0), (0, 1), (1, 1), (1, 0)]),
    (0, -1, 0): ([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], [(0, 0), (1, 0), (1, 1), (0, 1)]),
    (0, 0, 1):  ([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    (0, 0, -1): ([(1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)], [(1, 1), (0, 1), (0, 0), (1, 0)]),
}
_SHADE = {(0, 1, 0): 1.0, (0, -1, 0): 0.5, (1, 0, 0): 0.6, (-1, 0, 0): 0.6,
          (0, 0, 1): 0.8, (0, 0, -1): 0.8}
_FACE_LIST = list(_FACES.items())

_CROSS_QUADS = [
    ([(.05, 0, .05), (.95, 0, .95), (.95, 1, .95), (.05, 1, .05)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    ([(.05, 0, .05), (.05, 1, .05), (.95, 1, .95), (.95, 0, .95)], [(0, 1), (0, 0), (1, 0), (1, 1)]),
    ([(.95, 0, .05), (.05, 0, .95), (.05, 1, .95), (.95, 1, .05)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    ([(.95, 0, .05), (.95, 1, .05), (.05, 1, .95), (.05, 0, .95)], [(0, 1), (0, 0), (1, 0), (1, 1)]),
]
_CROPS_QUADS = []
for _c in (0.25, 0.75):
    _CROPS_QUADS += [
        ([(_c, 0, 0), (_c, 0, 1), (_c, 1, 1), (_c, 1, 0)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
        ([(_c, 0, 0), (_c, 1, 0), (_c, 1, 1), (_c, 0, 1)], [(0, 1), (0, 0), (1, 0), (1, 1)]),
        ([(0, 0, _c), (1, 0, _c), (1, 1, _c), (0, 1, _c)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
        ([(0, 0, _c), (0, 1, _c), (1, 1, _c), (1, 0, _c)], [(0, 1), (0, 0), (1, 0), (1, 1)]),
    ]
_RAIL_Y = 1.0 / 16.0
_RAIL_QUADS = [
    ([(0, _RAIL_Y, 0), (0, _RAIL_Y, 1), (1, _RAIL_Y, 1), (1, _RAIL_Y, 0)], [(0, 0), (0, 1), (1, 1), (1, 0)]),
    ([(0, _RAIL_Y, 0), (1, _RAIL_Y, 0), (1, _RAIL_Y, 1), (0, _RAIL_Y, 1)], [(0, 0), (1, 0), (1, 1), (0, 1)]),
]
# ladder wall quad by metadata (2=-Z,3=+Z,4=-X,5=+X); 0.05 off the wall
_LADDER_QUADS = {
    2: ([(1, 0, .95), (0, 0, .95), (0, 1, .95), (1, 1, .95)], [(1, 1), (0, 1), (0, 0), (1, 0)]),
    3: ([(0, 0, .05), (1, 0, .05), (1, 1, .05), (0, 1, .05)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    4: ([(.95, 0, 0), (.95, 0, 1), (.95, 1, 1), (.95, 1, 0)], [(0, 1), (1, 1), (1, 0), (0, 0)]),
    5: ([(.05, 0, 1), (.05, 0, 0), (.05, 1, 0), (.05, 1, 1)], [(1, 1), (0, 1), (0, 0), (1, 0)]),
}


def _padded_ids(world, cx, cz):
    here = world.chunks.get((cx, cz))
    if here is None:
        return None, None
    pad = np.zeros((CHUNK_X + 2, CHUNK_Z + 2, CHUNK_Y + 2), dtype=np.uint8)
    pad[1:-1, 1:-1, 1:-1] = here
    nx = world.chunks.get((cx + 1, cz));  pad[-1, 1:-1, 1:-1] = nx[0, :, :] if nx is not None else 0
    px = world.chunks.get((cx - 1, cz));  pad[0, 1:-1, 1:-1] = px[-1, :, :] if px is not None else 0
    nz = world.chunks.get((cx, cz + 1));  pad[1:-1, -1, 1:-1] = nz[:, 0, :] if nz is not None else 0
    pz = world.chunks.get((cx, cz - 1));  pad[1:-1, 0, 1:-1] = pz[:, -1, :] if pz is not None else 0
    return here, pad


def _atlas_uv(tiles, corner_uv):
    col = (tiles % _ATLAS).astype(np.float32); row = (tiles // _ATLAS).astype(np.float32)
    u0 = col * _TSZE + _INSET; u1 = (col + 1) * _TSZE - _INSET
    v0 = row * _TSZE + _INSET; v1 = (row + 1) * _TSZE - _INSET
    uv = np.empty((len(tiles), 4, 2), np.float32)
    for k, (tu, tv) in enumerate(corner_uv):
        uv[:, k, 0] = u1 if tu else u0
        uv[:, k, 1] = v1 if tv else v0
    return uv


def _quad(parts, base, corner_xyz, corner_uv, tiles, colors):
    p = base[:, None, :] + np.array(corner_xyz, np.float32)[None, :, :]
    parts[0].append(p.reshape(-1, 3))
    parts[1].append(_atlas_uv(tiles, corner_uv).reshape(-1, 2))
    parts[2].append(np.repeat(colors, 4, axis=0))


def _batch(parts, here, xs, zs, ys, ox, oz, quads, shade, kind=1, metas=None):
    """Emit a fixed list of quads for every voxel in (xs,zs,ys). metas (aligned
    to xs) selects metadata-driven tiles (wool colour, plant type, ...)."""
    if not len(xs):
        return
    ids = here[xs, zs, ys]
    base = np.stack([xs + ox, ys, zs + oz], axis=1).astype(np.float32)
    tile = _TILE_LUT[ids, (metas if metas is not None else 0), kind]
    tint = _TINT[ids, kind] * shade
    for corner_xyz, corner_uv in quads:
        _quad(parts, base, corner_xyz, corner_uv, tile, tint)


def _box(parts, base, ext, tiles3, tint_base, cull=None):
    """Emit the 6 faces of an AABB box (ext=x0,y0,z0,x1,y1,z1). tiles3 is a
    (top,side,bottom) tile tuple (or a bare int for all faces). cull: dict
    normal->bool mask (M,) of faces to skip (flush against opaque)."""
    x0, y0, z0, x1, y1, z1 = ext
    for (dx, dy, dz), (corner_xyz, corner_uv) in _FACE_LIST:
        tile = tiles3[0 if dy == 1 else 2 if dy == -1 else 1] if isinstance(tiles3, tuple) else tiles3
        m = cull.get((dx, dy, dz)) if cull else None
        boxc = [(x0 if c[0] == 0 else x1, y0 if c[1] == 0 else y1, z0 if c[2] == 0 else z1)
                for c in corner_xyz]
        b = base if m is None else base[~m]
        if len(b) == 0:
            continue
        n = len(b)
        _quad(parts, b, boxc, corner_uv,
              np.full(n, tile, np.int32), np.tile(tint_base * _SHADE[(dx, dy, dz)], (n, 1)))


def _emit_quad(parts, verts, uv, color3, double=False):
    """Append one quad with explicit world positions + explicit atlas UVs."""
    v = np.asarray(verts, np.float32).reshape(4, 3)
    uv = np.asarray(uv, np.float32).reshape(4, 2)
    col = np.tile(np.asarray(color3, np.float32), (4, 1))
    parts[0].append(v); parts[1].append(uv); parts[2].append(col)
    if double:                                   # back face (torch has no winding)
        parts[0].append(v[::-1]); parts[1].append(uv[::-1]); parts[2].append(col)


def _torch(parts, tile, bottom, top, tint):
    """Draw a torch as a 2px-wide tilted prism whose faces sample only the torch
    tile's centre strip (the stick), with the flame tile region on top. bottom
    and top are world-space centre points of the stick's base and tip."""
    hw = 1.0 / 16.0
    col = int(tile) % _ATLAS; row = int(tile) // _ATLAS

    def rect(u0, v0, u1, v1, order):             # sub-tile rectangle -> atlas uv
        a = {'tl': ((col + u0) * _TSZE, (row + v0) * _TSZE),
             'tr': ((col + u1) * _TSZE, (row + v0) * _TSZE),
             'br': ((col + u1) * _TSZE, (row + v1) * _TSZE),
             'bl': ((col + u0) * _TSZE, (row + v1) * _TSZE)}
        return [a[o] for o in order]

    bx, by, bz = bottom; tx, ty, tz = top
    offs = [(-hw, -hw), (hw, -hw), (hw, hw), (-hw, hw)]
    for i in range(4):                           # 4 side faces (stick strip)
        ax, az = offs[i]; cx_, cz_ = offs[(i + 1) % 4]
        verts = [(bx + ax, by, bz + az), (bx + cx_, by, bz + cz_),
                 (tx + cx_, ty, tz + cz_), (tx + ax, ty, tz + az)]
        _emit_quad(parts, verts, rect(7/16, 6/16, 9/16, 1.0, ['bl', 'br', 'tr', 'tl']),
                   tint, double=True)
    verts = [(tx - hw, ty, tz - hw), (tx + hw, ty, tz - hw),   # top (flame tip)
             (tx + hw, ty, tz + hw), (tx - hw, ty, tz + hw)]
    _emit_quad(parts, verts, rect(7/16, 6/16, 9/16, 8/16, ['tl', 'tr', 'br', 'bl']),
               tint, double=True)


# wall-torch metadata -> (base centre, tip centre) in 0..1 block coords; the tip
# leans out toward the block centre so the torch angles off the wall.
_TORCH_POSE = {
    1: ((1/16, 4/16, 0.5),  (7/16, 13/16, 0.5)),   # on west (-X) wall, leans +X
    2: ((15/16, 4/16, 0.5), (9/16, 13/16, 0.5)),   # on east (+X) wall, leans -X
    3: ((0.5, 4/16, 1/16),  (0.5, 13/16, 7/16)),   # on north (-Z) wall, leans +Z
    4: ((0.5, 4/16, 15/16), (0.5, 13/16, 9/16)),   # on south (+Z) wall, leans -Z
}
_TORCH_FLOOR = ((0.5, 0.0, 0.5), (0.5, 10/16, 0.5))


def build_chunk_mesh(world, cx, cz):
    here, pad = _padded_ids(world, cx, cz)
    if here is None:
        return None
    render = _RENDER[here]
    if not render.any():
        return None
    meta = world.data.get((cx, cz))
    metafull = meta if meta is not None else np.zeros((CHUNK_X, CHUNK_Z, CHUNK_Y), np.uint8)
    cat = _CAT[here]
    grp_here = _GRP[here]
    ox, oz = cx * CHUNK_X, cz * CHUNK_Z
    parts = ([], [], [])

    # ---- full-cube pass (vectorised, with opacity + same-group culling) ----
    cube = render & (cat == _CUBE)
    for (dx, dy, dz), (corner_xyz, corner_uv) in _FACE_LIST:
        nid = pad[1 + dx:1 + dx + CHUNK_X, 1 + dz:1 + dz + CHUNK_Z, 1 + dy:1 + dy + CHUNK_Y]
        same = (grp_here != 0) & (grp_here == _GRP[nid])
        visible = cube & ~_OPAQUE[nid] & ~same
        if not visible.any():
            continue
        xs, zs, ys = np.nonzero(visible)
        ids = here[xs, zs, ys]
        kind = 0 if dy == 1 else 2 if dy == -1 else 1
        base = np.stack([xs + ox, ys, zs + oz], axis=1).astype(np.float32)
        _quad(parts, base, corner_xyz, corner_uv, _TILE_LUT[ids, metafull[xs, zs, ys], kind],
              _TINT[ids, kind] * _SHADE[(dx, dy, dz)])

    # ---- batched special shapes ----
    def vox(c):
        return np.nonzero(render & (cat == c))
    cxs, czs, cys = vox(_CROSS)
    _batch(parts, here, cxs, czs, cys, ox, oz, _CROSS_QUADS, 0.9,
           metas=metafull[cxs, czs, cys])          # sapling/tall-grass type by meta
    _batch(parts, here, *vox(_CROPS), ox, oz, _CROPS_QUADS, 0.9)

    # rails: flat quad, or a ramp for sloped metadata (2..5)
    rxs, rzs, rys = vox(_RAIL)
    yb = 1.0 / 16.0
    for x, z, y in zip(rxs.tolist(), rzs.tolist(), rys.tolist()):
        bid = int(here[x, z, y]); m = int(meta[x, z, y]) if meta is not None else 0
        h = [yb, yb, yb, yb]                                  # corners (0,0)(1,0)(1,1)(0,1)
        if bid in (27, 66):
            if m == 2:   h[1] = h[2] = yb + 1                 # ascend +X
            elif m == 3: h[0] = h[3] = yb + 1                 # ascend -X
            elif m == 4: h[0] = h[1] = yb + 1                 # ascend -Z
            elif m == 5: h[2] = h[3] = yb + 1                 # ascend +Z
        base = np.array([[x + ox, y, z + oz]], np.float32)
        cor = [(0, h[0], 0), (1, h[1], 0), (1, h[2], 1), (0, h[3], 1)]
        tile = np.array([int(_TILE_LUT[bid, m, 1])], np.int32); tint = _TINT[bid, 1][None, :]
        _quad(parts, base, cor, [(0, 0), (1, 0), (1, 1), (0, 1)], tile, tint)
        _quad(parts, base, cor[::-1], [(0, 1), (1, 1), (1, 0), (0, 0)], tile, tint)

    # torches: a thin tilted prism (wall metas lean off their wall)
    txs, tzs, tys = vox(_TORCH)
    for x, z, y in zip(txs.tolist(), tzs.tolist(), tys.tolist()):
        bid = int(here[x, z, y]); m = int(metafull[x, z, y])
        b, t = _TORCH_POSE.get(m, _TORCH_FLOOR)
        bottom = (x + ox + b[0], y + b[1], z + oz + b[2])
        top =    (x + ox + t[0], y + t[1], z + oz + t[2])
        _torch(parts, int(_TILE_LUT[bid, m, 1]), bottom, top, _TINT[bid, 1])

    # ladders: one wall quad per metadata orientation
    lxs, lzs, lys = vox(_LADDER)
    if len(lxs):
        lm = (meta[lxs, lzs, lys] if meta is not None else np.full(len(lxs), 4)).astype(int)
        for mval, quad in _LADDER_QUADS.items():
            sel = lm == mval
            _batch(parts, here, lxs[sel], lzs[sel], lys[sel], ox, oz, [quad], 1.0)
        # default (no matching wall meta) -> render on -X
        other = ~np.isin(lm, list(_LADDER_QUADS))
        _batch(parts, here, lxs[other], lzs[other], lys[other], ox, oz, [_LADDER_QUADS[4]], 1.0)

    # ---- box blocks (slabs, stairs, fence post, cactus, snow, ...) ----
    bxs, bzs, bys = vox(_BOX)
    for x, z, y in zip(bxs.tolist(), bzs.tolist(), bys.tolist()):
        bid = int(here[x, z, y])
        m = int(meta[x, z, y]) if meta is not None else 0
        if bid in _STAIR_IDS:
            boxlist = blocks.stairs_boxes(m)                  # oriented by metadata
        elif bid == 68:                                       # wall sign -> flush on its wall
            boxlist = blocks.wall_sign_boxes(m)
        elif bid == 63:                                       # standing sign -> post + board
            boxlist = blocks.sign_post_boxes(m)
        elif bid in (96, 167):                                # trap doors -> by facing/open
            boxlist = blocks.trapdoor_boxes(m)
        elif bid in (64, 71):                                 # doors -> by facing (from lower half)
            mb = int(meta[x, z, y - 1]) if (meta is not None and y > 0) else 0
            boxlist = blocks.door_boxes(m, mb)
        elif bid == 85:                                       # fence: post + rails
            boxlist = [(6/16, 0, 6/16, 10/16, 1, 10/16)]
            xm = pad[x, 1+z, 1+y] == 85; xp = pad[2+x, 1+z, 1+y] == 85
            zm = pad[1+x, z, 1+y] == 85; zp = pad[1+x, 2+z, 1+y] == 85
            cx_, cz_ = xm or xp, zm or zp
            if not cx_ and not cz_:
                cx_ = True                                    # lone post -> X rails
            if cx_:
                x0, x1 = (0 if xm else 7/16), (1 if xp else 9/16)
                boxlist += [(x0, 12/16, 7/16, x1, 15/16, 9/16),
                            (x0, 6/16, 7/16, x1, 9/16, 9/16)]
            if cz_:
                z0, z1 = (0 if zm else 7/16), (1 if zp else 9/16)
                boxlist += [(7/16, 12/16, z0, 9/16, 15/16, z1),
                            (7/16, 6/16, z0, 9/16, 9/16, z1)]
        else:
            boxlist = blocks.STATIC_BOXES.get(bid, [(0, 0, 0, 1, 1, 1)])
        base = np.array([[x + ox, y, z + oz]], np.float32)
        tile = (int(_TILE_LUT[bid, m, 0]), int(_TILE_LUT[bid, m, 1]), int(_TILE_LUT[bid, m, 2]))
        tint = _TINT[bid, 1]
        for ext in boxlist:
            # cull faces flush against an opaque neighbour (kills slab-bottom z-fight)
            cull = {}
            for (dx, dy, dz) in _FACES:
                at_bound = ((dx == 1 and ext[3] == 1) or (dx == -1 and ext[0] == 0) or
                            (dy == 1 and ext[4] == 1) or (dy == -1 and ext[1] == 0) or
                            (dz == 1 and ext[5] == 1) or (dz == -1 and ext[2] == 0))
                if at_bound and _OPAQUE[pad[1 + x + dx, 1 + z + dz, 1 + y + dy]]:
                    cull[(dx, dy, dz)] = np.array([True])
            _box(parts, base, ext, tile, tint, cull)

    if not parts[0]:
        return None
    positions = np.concatenate(parts[0])
    uvs = np.concatenate(parts[1])
    colors = np.concatenate(parts[2])
    nfaces = len(positions) // 4
    quad = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)
    indices = (np.arange(nfaces, dtype=np.uint32)[:, None] * 4 + quad[None, :]).reshape(-1)
    return positions, uvs, colors, indices


def build_world_mesh(world):
    pos_parts, uv_parts, col_parts, idx_parts = [], [], [], []
    base = 0
    for (cx, cz) in world.chunks:
        m = build_chunk_mesh(world, cx, cz)
        if not m:
            continue
        positions, uvs, colors, indices = m
        pos_parts.append(positions); uv_parts.append(uvs); col_parts.append(colors)
        idx_parts.append(indices + base)
        base += len(positions)
    if not pos_parts:
        z = np.zeros((0, 3), np.float32)
        return z, np.zeros((0, 2), np.float32), z, np.zeros((0,), np.uint32)
    return (np.concatenate(pos_parts), np.concatenate(uv_parts),
            np.concatenate(col_parts), np.concatenate(idx_parts))


if __name__ == "__main__":
    import sys, time
    from world import World
    w = World.load(sys.argv[1])
    t = time.time()
    pos, uv, col, idx = build_world_mesh(w)
    print("world mesh: %d vertices | %d triangles | %.1fs"
          % (len(pos), len(idx) // 3, time.time() - t))
