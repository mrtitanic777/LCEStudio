"""Turn images and 3D primitives into buildable block structures.

    from lce import artgen
    b, d = artgen.image_to_arrays("logo.png", width=96)          # arrays to stamp
    artgen.image_to_schematic("logo.png", "logo.schematic", width=96)
    artgen.image_to_schematic("photo.png", "relief.schematic", depth=8)   # bas-relief
    artgen.sphere(12, "orb.schematic", block=(35, 11))

Pixels are matched to a curated palette of SOLID building blocks using their real
atlas colours. `palette=` picks a preset ('full' = ~60 blocks incl. 16 wool for the
widest colour range, 'wool' = the 16 clean wool hues, 'grays' = greyscale for B&W
photos, 'concrete' = an extended set for newer/Aquatic worlds). `depth>1` extrudes a
bas-relief (wall) or a heightmap (floor) from image luminance.
"""
import os

import numpy as np
from PIL import Image

from . import schematic as S
from .view3d import mesher

_ATLAS = os.path.join(os.path.dirname(__file__), "view3d", "textures", "terrain.png")

# ---- palettes: lists of (id, meta) solid blocks -----------------------------
_WOOL = [(35, m) for m in range(16)]
# every opaque solid Beta full-block with a usable colour (widest range)
_FULL = _WOOL + [(1, 0), (4, 0), (48, 0), (98, 0), (45, 0), (24, 0), (43, 0), (5, 0),
                 (17, 0), (18, 0), (2, 0), (3, 0), (12, 0), (13, 0), (19, 0), (21, 0),
                 (22, 0), (23, 0), (25, 0), (41, 0), (42, 0), (57, 0), (46, 0), (47, 0),
                 (49, 0), (54, 0), (56, 0), (58, 0), (61, 0), (73, 0), (7, 0), (79, 0),
                 (80, 0), (82, 0), (84, 0), (86, 0), (87, 0), (88, 0), (89, 0), (91, 0),
                 (95, 0), (14, 0), (15, 0), (16, 0)]
_GRAYS = [(80, 0), (42, 0), (43, 0), (98, 0), (1, 0), (4, 0), (48, 0), (7, 0), (49, 0)]
# FLAT-surface blocks only (no busy texture, no light-emitters) -> smooth pixel art.
# wool hues MINUS lime(5) so greens consolidate to one solid green (13) — no
# light/dark-green speckle; + gold/iron/diamond/lapis blocks + snow/obsidian/clay
_SMOOTH = [(35, m) for m in range(16) if m != 5] + [(41, 0), (42, 0), (57, 0),
                                                    (22, 0), (80, 0), (49, 0), (82, 0)]
# extended set for NEWER (Aquatic) worlds: LCE concrete ids 236..251 (16 colours).
# Colours are approximate RGB (the Beta atlas has no concrete texture, so these won't
# preview right in the viewer, but render correctly IN-GAME on an Aquatic world).
_CONCRETE_RGB = [
    (207, 213, 214), (224, 97, 1), (169, 48, 159), (36, 137, 199), (241, 175, 21),
    (94, 169, 24), (214, 101, 143), (55, 58, 62), (125, 125, 115), (21, 119, 136),
    (100, 32, 156), (45, 47, 143), (96, 60, 32), (73, 91, 36), (142, 33, 33), (8, 10, 15),
]


def _palette_rgb(pal):
    a = np.asarray(Image.open(_ATLAS).convert("RGBA"))
    out = np.zeros((len(pal), 3), np.float64)
    for i, (bid, meta) in enumerate(pal):
        t = int(mesher._TILE_LUT[bid, meta, 0])
        col, row = t % 16, t // 16
        patch = a[row * 16:(row + 1) * 16, col * 16:(col + 1) * 16]
        mask = patch[:, :, 3] > 128
        rgb = patch[:, :, :3][mask].mean(axis=0) if mask.any() else np.zeros(3)
        out[i] = np.clip(rgb * mesher._TINT[bid, 0], 0, 255)
    return out


def _resolve_palette(name):
    """Return (blocks [(id,meta)], colours [N,3])."""
    if name == "wool":
        pal = _WOOL
    elif name == "smooth":
        pal = _SMOOTH
    elif name == "grays":
        pal = _GRAYS
    elif name == "concrete":                       # concrete ids 236..251 + wool fallback
        pal = [(236 + i, 0) for i in range(16)] + _WOOL
        rgb = np.vstack([np.array(_CONCRETE_RGB, np.float64), _palette_rgb(_WOOL)])
        return pal, rgb
    else:
        pal = _FULL
    return pal, _palette_rgb(pal)


def _nearest(arr, pcol):
    d = ((arr[:, :, None, :] - pcol[None, None, :, :]) ** 2).sum(3)
    return d.argmin(2)


def _dither(arr, pcol):
    H, W, _ = arr.shape
    work = arr.astype(np.float64).copy()
    idx = np.zeros((H, W), np.int32)
    for y in range(H):
        for x in range(W):
            old = work[y, x]
            i = int(((pcol - old) ** 2).sum(1).argmin())
            idx[y, x] = i
            err = old - pcol[i]
            if x + 1 < W:      work[y, x + 1] += err * (7 / 16)
            if y + 1 < H:
                if x > 0:      work[y + 1, x - 1] += err * (3 / 16)
                work[y + 1, x] += err * (5 / 16)
                if x + 1 < W:  work[y + 1, x + 1] += err * (1 / 16)
    return idx


def image_to_arrays(img_path, width=64, orientation="wall", dither=False,
                    depth=1, palette="full", invert_depth=False, remove_bg=False,
                    saturation=1.0, contrast=1.0):
    """Image -> (blocks, data) np.uint8 [W,H,L] (x,y,z). Transparent pixels (alpha
    <128) become AIR so only the subject is built (no background wall). remove_bg
    also drops pixels matching the corner colour for images with no alpha. depth>1
    makes a bas-relief (wall, extruded in Z by luminance) or a heightmap (floor).
    saturation/contrast (>1 boosts) vivify colours BEFORE block matching, so bright
    hues pop against greys instead of matching a dull/dark block."""
    img = Image.open(img_path).convert("RGBA")
    width = max(1, min(256, int(width)))
    h = max(1, round(img.height * width / img.width))
    img = img.resize((width, h), Image.LANCZOS)
    if saturation != 1.0 or contrast != 1.0:                   # vivify (keep the alpha)
        from PIL import ImageEnhance
        rgb = img.convert("RGB")
        if saturation != 1.0:
            rgb = ImageEnhance.Color(rgb).enhance(saturation)
        if contrast != 1.0:
            rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
        img = Image.merge("RGBA", (*rgb.split(), img.split()[3]))
    rgba = np.asarray(img, np.float64)
    arr = rgba[:, :, :3]
    keep = rgba[:, :, 3] >= 128                                 # opaque pixels only
    if remove_bg:                                               # key out the corner colour
        bg = rgba[0, 0, :3]
        keep &= (np.abs(arr - bg).sum(2) > 40)
    pal, pcol = _resolve_palette(palette)
    idxmap = _dither(arr, pcol) if dither else _nearest(arr, pcol)
    depth = max(1, min(64, int(depth)))
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]) / 255.0
    if invert_depth:
        lum = 1.0 - lum
    dep = 1 + np.round((depth - 1) * lum).astype(int)          # [h,w] in 1..depth

    W, H = width, h
    if orientation == "floor":
        b = np.zeros((W, depth, H), np.uint8); d = np.zeros((W, depth, H), np.uint8)
        for row in range(H):
            for col in range(W):
                if not keep[row, col]:
                    continue
                bid, meta = pal[idxmap[row, col]]
                b[col, 0:dep[row, col], row] = bid; d[col, 0:dep[row, col], row] = meta
    else:                                                       # wall (bas-relief in Z)
        b = np.zeros((W, H, depth), np.uint8); d = np.zeros((W, H, depth), np.uint8)
        for row in range(H):
            for col in range(W):
                if not keep[row, col]:
                    continue
                bid, meta = pal[idxmap[row, col]]
                y = H - 1 - row; z0 = depth - dep[row, col]     # brighter protrudes forward
                b[col, y, z0:depth] = bid; d[col, y, z0:depth] = meta
    return b, d


def image_to_schematic(img_path, out_path, **kw):
    b, d = image_to_arrays(img_path, **kw)
    S.save(b, d, out_path, name=os.path.splitext(os.path.basename(out_path))[0])
    return b.shape


def scale_arrays(b, d, factor):
    """Resample block/data arrays by `factor` (0<factor<=4). Each output cell takes
    the most-common non-air block in its source region (mode = keeps solid shapes)."""
    W, H, L = b.shape
    nW = max(1, round(W * factor)); nH = max(1, round(H * factor)); nL = max(1, round(L * factor))
    nb = np.zeros((nW, nH, nL), np.uint8); nd = np.zeros((nW, nH, nL), np.uint8)
    for x in range(nW):
        x0 = int(x / factor); x1 = min(W, max(x0 + 1, round((x + 1) / factor)))
        for y in range(nH):
            y0 = int(y / factor); y1 = min(H, max(y0 + 1, round((y + 1) / factor)))
            for z in range(nL):
                z0 = int(z / factor); z1 = min(L, max(z0 + 1, round((z + 1) / factor)))
                rb = b[x0:x1, y0:y1, z0:z1].ravel(); rd = d[x0:x1, y0:y1, z0:z1].ravel()
                nz = rb != 0
                if not nz.any():
                    continue
                key = rb[nz].astype(np.int32) | (rd[nz].astype(np.int32) << 8)
                vals, counts = np.unique(key, return_counts=True)
                k = int(vals[counts.argmax()])
                nb[x, y, z] = k & 0xFF; nd[x, y, z] = (k >> 8) & 0xF
    return nb, nd


_FONT_CANDIDATES = [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
                    r"C:\Windows\Fonts\segoeui.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"]


def text_to_arrays(text, block=(35, 15), height=12, font_path=None, outline=None):
    """Render `text` to block letters -> (blocks, data) [W,H,1] wall. `height` = cap
    height in blocks. `outline` = an (id,meta) 1-block border around the glyphs."""
    from PIL import ImageFont, ImageDraw
    fp = font_path
    if fp is None:
        for c in _FONT_CANDIDATES:
            if os.path.exists(c):
                fp = c; break
    size = max(8, int(height * 1.35))
    try:
        font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    x0, y0, x1, y1 = tmp.textbbox((0, 0), text, font=font)
    pad = 2 if outline else 1
    img = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0)
    ImageDraw.Draw(img).text((pad - x0, pad - y0), text, fill=255, font=font)
    mask = np.asarray(img) > 100
    H, W = mask.shape
    bid, meta = block
    b = np.zeros((W, H, 1), np.uint8); d = np.zeros((W, H, 1), np.uint8)
    if outline:
        oid, ometa = outline
        grown = mask.copy()
        grown[1:, :] |= mask[:-1, :]; grown[:-1, :] |= mask[1:, :]
        grown[:, 1:] |= mask[:, :-1]; grown[:, :-1] |= mask[:, 1:]
        edge = grown & ~mask
        for row in range(H):
            for col in range(W):
                if edge[row, col]:
                    b[col, H - 1 - row, 0] = oid; d[col, H - 1 - row, 0] = ometa
    for row in range(H):
        for col in range(W):
            if mask[row, col]:
                b[col, H - 1 - row, 0] = bid; d[col, H - 1 - row, 0] = meta
    return b, d


def text_to_schematic(text, out_path, **kw):
    b, d = text_to_arrays(text, **kw)
    S.save(b, d, out_path, name=os.path.splitext(os.path.basename(out_path))[0])
    return b.shape


def scale_schematic(in_path, out_path, factor):
    """Scale an existing .schematic by `factor` (e.g. 0.5 = half size)."""
    b, d, _tes = S.load(in_path)
    nb, nd = scale_arrays(b, d, float(factor))
    S.save(nb, nd, out_path, name=os.path.splitext(os.path.basename(out_path))[0])
    return b.shape, nb.shape


# -------------------------------------------------------------- 3D primitives
def sphere_arrays(radius, block=(35, 14), hollow=True):
    r = int(radius); n = 2 * r + 1
    b = np.zeros((n, n, n), np.uint8); d = np.zeros((n, n, n), np.uint8)
    bid, meta = block
    for x in range(n):
        for y in range(n):
            for z in range(n):
                dist2 = (x - r) ** 2 + (y - r) ** 2 + (z - r) ** 2
                if dist2 > (r + 0.5) ** 2 or (hollow and dist2 < (r - 0.5) ** 2):
                    continue
                b[x, y, z] = bid; d[x, y, z] = meta
    return b, d


def cylinder_arrays(radius, height, block=(35, 9), hollow=False):
    r = int(radius); n = 2 * r + 1; hgt = int(height)
    b = np.zeros((n, hgt, n), np.uint8); d = np.zeros((n, hgt, n), np.uint8)
    bid, meta = block
    for x in range(n):
        for z in range(n):
            dist2 = (x - r) ** 2 + (z - r) ** 2
            if dist2 > (r + 0.5) ** 2 or (hollow and dist2 < (r - 0.5) ** 2):
                continue
            b[x, :, z] = bid; d[x, :, z] = meta
    return b, d


def pyramid_arrays(base, block=(24, 0), solid=True):
    n = int(base); levels = (n + 1) // 2
    b = np.zeros((n, levels, n), np.uint8); d = np.zeros((n, levels, n), np.uint8)
    bid, meta = block
    for lv in range(levels):
        lo, hi = lv, n - 1 - lv
        for x in range(lo, hi + 1):
            for z in range(lo, hi + 1):
                if solid or x in (lo, hi) or z in (lo, hi):
                    b[x, lv, z] = bid; d[x, lv, z] = meta
    return b, d


def _save(b, d, out_path):
    S.save(b, d, out_path, name=os.path.splitext(os.path.basename(out_path))[0])
    return b.shape


def sphere(radius, out_path, block=(35, 14), hollow=True):
    return _save(*sphere_arrays(radius, block, hollow), out_path=out_path)


def cylinder(radius, height, out_path, block=(35, 9), hollow=False):
    return _save(*cylinder_arrays(radius, height, block, hollow), out_path=out_path)


def pyramid(base, out_path, block=(24, 0), solid=True):
    return _save(*pyramid_arrays(base, block, solid), out_path=out_path)


# -------------------------------------------------------------- preview
def preview_png(schem_path, out_png, scale=6):
    """Render a schematic's front face (max-Z projection) to a PNG."""
    b, d, _tes = S.load(schem_path)
    colcache = {}

    def rgb(bid, meta):
        key = (int(bid), int(meta))
        if key not in colcache:
            if key[0] >= 236:                          # concrete-era: approximate colour
                colcache[key] = np.array(_CONCRETE_RGB[(key[0] - 236) % 16], np.uint8)
            else:
                colcache[key] = _palette_rgb([key])[0].astype(np.uint8)
        return colcache[key]

    W, H, L = b.shape
    img = np.zeros((H, W, 3), np.uint8)
    for x in range(W):
        for y in range(H):
            for z in range(L - 1, -1, -1):
                if b[x, y, z]:
                    img[H - 1 - y, x] = rgb(b[x, y, z], d[x, y, z]); break
    im = Image.fromarray(img, "RGB")
    if scale != 1:
        im = im.resize((W * scale, H * scale), Image.NEAREST)
    im.save(out_png)
    return im
