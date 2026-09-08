"""Voxelize a 3D model (.obj) into a coloured block structure (a .schematic).

    from lce import voxelize
    voxelize.obj_to_schematic("model.obj", "model.schematic", size=48)

Surface-voxelizes each triangle (watertight at the target resolution), colours each
voxel from its face's material diffuse colour (from the .mtl, if any) matched to the
nearest building block, and optionally fills the interior. Y is treated as up.
"""
import os

import numpy as np

from .. import schematic as S
from .. import artgen


def _load_mtl(path):
    mats, cur = {}, None
    if not os.path.exists(path):
        return mats
    for line in open(path, "r", errors="ignore"):
        p = line.split()
        if not p:
            continue
        if p[0] == "newmtl":
            cur = p[1]
        elif p[0] == "Kd" and cur is not None:
            mats[cur] = tuple(float(x) * 255 for x in p[1:4])
    return mats


def load_obj(path):
    """Return (verts [N,3] float, faces [(i0,i1,i2,color|None)])."""
    verts, faces, mats = [], [], {}
    cur = None
    base = os.path.dirname(path)
    for line in open(path, "r", errors="ignore"):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif p[0] == "mtllib":
            mats.update(_load_mtl(os.path.join(base, " ".join(p[1:]))))
        elif p[0] == "usemtl":
            cur = mats.get(p[1])
        elif p[0] == "f":
            idx = []
            for tok in p[1:]:
                i = int(tok.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):        # triangulate a polygon fan
                faces.append((idx[0], idx[k], idx[k + 1], cur))
    return np.array(verts, np.float64), faces


def voxelize(verts, faces, size=48):
    """Surface-voxelize -> (grid_id uint8 [gx,gy,gz], grid_col float [..,3], dims)."""
    mn, mx = verts.min(0), verts.max(0)
    ext = np.maximum(mx - mn, 1e-9)
    scale = (size - 1) / ext.max()
    v = (verts - mn) * scale
    dims = (np.floor(ext * scale).astype(int) + 1)
    gid = np.zeros(tuple(dims), np.uint8)
    gcol = np.zeros((*dims, 3), np.float64)
    dmax = dims - 1
    for i0, i1, i2, color in faces:
        p0, p1, p2 = v[i0], v[i1], v[i2]
        n = int(max(np.linalg.norm(p1 - p0), np.linalg.norm(p2 - p0),
                    np.linalg.norm(p2 - p1))) * 2 + 2
        c = np.array(color if color else (170, 170, 170), np.float64)
        for a in range(n + 1):
            for bb in range(n + 1 - a):
                u, w = a / n, bb / n
                pt = p0 * (1 - u - w) + p1 * u + p2 * w
                g = np.clip(np.round(pt).astype(int), 0, dmax)
                gid[g[0], g[1], g[2]] = 1
                gcol[g[0], g[1], g[2]] = c
    return gid, gcol, dims


def _fill_interior(gid):
    """Approximate solid fill: along Y, fill between the lowest and highest surface
    voxel in each (x,z) column."""
    out = gid.copy()
    gx, gy, gz = gid.shape
    for x in range(gx):
        for z in range(gz):
            ys = np.nonzero(gid[x, :, z])[0]
            if len(ys) >= 2:
                out[x, ys[0]:ys[-1] + 1, z] = 1
    return out


def obj_to_arrays(obj_path, size=48, block=None, solid=False, palette="full"):
    """(blocks, data) for a voxelized .obj. block=(id,meta) forces one block; else
    colours come from the model's materials matched to the nearest palette block."""
    verts, faces = load_obj(obj_path)
    if len(verts) == 0 or not faces:
        raise ValueError("no geometry in %s" % obj_path)
    gid, gcol, dims = voxelize(verts, faces, size)
    if solid:
        gid = _fill_interior(gid)
    pal, pcol = artgen._resolve_palette(palette)
    b = np.zeros(tuple(dims), np.uint8); d = np.zeros(tuple(dims), np.uint8)
    for x, y, z in np.argwhere(gid):
        if block is not None:
            bid, meta = block
        else:
            col = gcol[x, y, z]
            if not col.any():                        # interior fill w/o a colour -> a neighbour's
                bid, meta = pal[int(((pcol - 170) ** 2).sum(1).argmin())]
            else:
                bid, meta = pal[int(((pcol - col) ** 2).sum(1).argmin())]
        b[x, y, z] = bid; d[x, y, z] = meta
    return b, d


def obj_to_schematic(obj_path, out_path, size=48, block=None, solid=False, palette="full"):
    b, d = obj_to_arrays(obj_path, size=size, block=block, solid=solid, palette=palette)
    S.save(b, d, out_path, name=os.path.splitext(os.path.basename(out_path))[0])
    return b.shape
