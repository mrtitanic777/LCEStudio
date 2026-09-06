"""Isometric world-portrait renderer.

Projects a world's surface (top block of every column, from the view3d numpy World)
into a classic 2:1 isometric image with lit tops and shaded cliff faces -- a
shareable 'portrait' rather than the flat top-down map. Painter's algorithm over the
diagonal, so height/cliffs read correctly.
"""
import numpy as np
from PIL import Image, ImageDraw

from . import atlas


def _heightmap(vw):
    """From a view3d World (chunks: {(cx,cz): uint8[16,16,256] as [x,z,y]}) build the
    per-column (top block id, top y) arrays over the whole world, indexed [x, z]."""
    from .view3d.world import CHUNK_Y
    xs = [c[0] for c in vw.chunks]; zs = [c[1] for c in vw.chunks]
    mincx, maxcx = min(xs), max(xs); mincz, maxcz = min(zs), max(zs)
    W = (maxcx - mincx + 1) * 16; H = (maxcz - mincz + 1) * 16
    top_id = np.zeros((W, H), np.uint8)
    top_h = np.full((W, H), -1, np.int16)
    ii, jj = np.indices((16, 16))
    for (cx, cz), arr in vw.chunks.items():
        nz = arr != 0                                # [x,z,y]
        any_s = nz.any(axis=2)
        ty = (CHUNK_Y - 1) - np.argmax(nz[:, :, ::-1], axis=2)
        tid = np.where(any_s, arr[ii, jj, ty], 0).astype(np.uint8)
        ty = np.where(any_s, ty, -1).astype(np.int16)
        gx0 = (cx - mincx) * 16; gz0 = (cz - mincz) * 16
        top_id[gx0:gx0 + 16, gz0:gz0 + 16] = tid
        top_h[gx0:gx0 + 16, gz0:gz0 + 16] = ty
    return top_id, top_h


def render_iso(vw, tile=8, bg=(20, 24, 32), max_dim=8000, log=None):
    """Render `vw` (a view3d World) to an isometric PIL.Image. `tile` = half-width of a
    block in px (bigger = higher detail + bigger image). Auto-shrinks `tile` to keep the
    image under `max_dim` px on a side."""
    if not vw.chunks:
        return Image.new("RGB", (64, 64), bg)
    colors = atlas.block_color_table().astype(np.float32)       # [256,3]
    top_id, top_h = _heightmap(vw)
    gen = top_h >= 0
    if not gen.any():
        return Image.new("RGB", (64, 64), bg)
    gx, gz = np.where(gen)                                       # crop to generated content
    top_id = top_id[gx.min():gx.max() + 1, gz.min():gz.max() + 1]
    top_h = top_h[gx.min():gx.max() + 1, gz.min():gz.max() + 1]
    W, H = top_id.shape
    gen = top_h >= 0
    base = int(top_h[gen].min())

    # keep the output sane: iso image is ~ (W+H)*tile wide
    while (W + H) * tile > max_dim and tile > 1:
        tile -= 1
    tw = tile; th = max(1, tile // 2); bh = max(1, tile // 2)

    # screen bounds (see module notes: side bottoms are height-independent)
    minx = -(H - 1) * tw - tw
    miny = int(((np.indices((W, H))[0] + np.indices((W, H))[1])[gen] * th
                - (top_h[gen] - base) * bh).min()) - th
    W_px = ((W - 1) + (H - 1)) * tw + 2 * tw + 1
    H_px = ((W - 1) + (H - 1)) * th + th - miny + 1
    OX, OY = -minx, -miny

    img = Image.new("RGB", (W_px, H_px), bg)
    dr = ImageDraw.Draw(img)

    def shade(bid, f):
        r, g, b = colors[bid]
        return (int(min(255, r * f)), int(min(255, g * f)), int(min(255, b * f)))

    # painter's order: back (small x+z) to front. Precompute diagonal buckets.
    order = sorted(((x, z) for x in range(W) for z in range(H) if top_h[x, z] >= 0),
                   key=lambda p: (p[0] + p[1], p[0]))
    if log:
        log("iso: %d columns, tile=%d, image %dx%d" % (len(order), tile, W_px, H_px))
    for x, z in order:
        h = int(top_h[x, z]); bid = int(top_id[x, z])
        cx = OX + (x - z) * tw
        cy = OY + (x + z) * th - (h - base) * bh
        # exposed cliff heights vs the two front-facing neighbours
        er = h - (int(top_h[x + 1, z]) if x + 1 < W and top_h[x + 1, z] >= 0 else base)
        el = h - (int(top_h[x, z + 1]) if z + 1 < H and top_h[x, z + 1] >= 0 else base)
        dR, dL = max(0, er) * bh, max(0, el) * bh
        # right face (toward +x): darker
        if dR:
            dr.polygon([(cx, cy + th), (cx + tw, cy), (cx + tw, cy + dR), (cx, cy + th + dR)],
                       fill=shade(bid, 0.72))
        # left face (toward +z): darkest
        if dL:
            dr.polygon([(cx - tw, cy), (cx, cy + th), (cx, cy + th + dL), (cx - tw, cy + dL)],
                       fill=shade(bid, 0.55))
        # top face (diamond): lit
        dr.polygon([(cx, cy - th), (cx + tw, cy), (cx, cy + th), (cx - tw, cy)],
                   fill=shade(bid, 1.0))
    return img
