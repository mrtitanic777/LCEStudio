"""Rendering engine for LCE Studio -- top-down world maps, horizontal slices,
and overlays (spawn/player/entities, the world boundary frame, and a chunk grid).
Uses Pillow. Coordinates are Minecraft world blocks.

    img, ox, oz = render_topdown(world, size=54)   # full 54x54 world square
    disp = overlay(img, ox, oz, scale=2, spawn=..., grid=True, boundary=True, size=54)

The image is sized to the FULL finite-world boundary (default 54x54 chunks = the
LCE Classic size, centred on the origin) so generated chunks sit inside the real
world square and the un-generated remainder is shaded.
"""
from PIL import Image, ImageDraw

# block id -> RGB. Reasonable palette for LCE/beta blocks; unknowns -> magenta.
BLOCK_RGB = {
    0: None,                       # air (transparent -> SKY)
    1: (127, 127, 127), 2: (95, 159, 53), 3: (134, 96, 67), 4: (115, 115, 115),
    5: (157, 128, 79), 7: (40, 40, 40), 8: (58, 91, 176), 9: (58, 91, 176),
    10: (214, 88, 20), 11: (214, 88, 20), 12: (219, 207, 142), 13: (136, 126, 126),
    14: (143, 140, 125), 15: (136, 130, 127), 16: (69, 69, 69), 17: (102, 81, 50),
    18: (58, 95, 40), 19: (182, 182, 92), 20: (200, 226, 240), 21: (30, 68, 140),
    24: (216, 203, 155), 35: (222, 222, 222), 37: (204, 204, 40), 38: (188, 48, 40),
    41: (249, 236, 79), 42: (222, 222, 222), 43: (162, 162, 162), 44: (162, 162, 162),
    45: (150, 97, 83), 46: (219, 92, 51), 47: (159, 132, 77), 48: (90, 108, 90),
    49: (26, 20, 34), 50: (245, 214, 100), 52: (46, 68, 82), 53: (157, 128, 79),
    54: (162, 130, 78), 56: (94, 124, 130), 57: (94, 219, 214), 58: (154, 123, 72),
    59: (146, 189, 74), 60: (110, 78, 52), 61: (100, 100, 100), 62: (110, 100, 95),
    63: (157, 128, 79), 64: (140, 110, 66), 78: (240, 251, 251), 79: (145, 183, 235),
    80: (240, 251, 251), 81: (56, 124, 30), 82: (159, 166, 179), 83: (94, 156, 62),
    85: (157, 128, 79), 89: (233, 204, 122), 95: (160, 120, 60),
    6: (76, 154, 60), 31: (89, 138, 51), 32: (123, 121, 73), 39: (150, 116, 92),
    40: (207, 78, 70), 51: (222, 130, 40), 55: (170, 40, 40), 65: (130, 104, 60),
    66: (150, 140, 130), 90: (110, 60, 160), 86: (211, 138, 40), 91: (215, 152, 62),
}
DEFAULT = (200, 40, 200)
SKY = (150, 180, 220)
UNGEN = (30, 33, 40)               # un-generated area inside the boundary


def _color(bid):
    return BLOCK_RGB.get(bid, DEFAULT)


def _overworld_chunks(world):
    """Yield (world_chunk_x, world_chunk_z, Chunk) for every loaded overworld chunk."""
    for name in [n for n in world._filedata if n.endswith(".mcr") and not n.startswith("DIM")]:
        rx, rz = (int(v) for v in name[:-4].split("/")[-1].split(".")[1:3])
        reg = world.region(rx, rz)
        reg.decode_all()
        for (lcx, lcz), ch in reg.chunks.items():
            yield rx * 32 + lcx, rz * 32 + lcz, ch


def _gen_chunk_bounds(world):
    xs, zs = [], []
    for wcx, wcz, _ in _overworld_chunks(world):
        xs.append(wcx); zs.append(wcz)
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(zs), max(xs), max(zs)


def world_bounds(world, size=54):
    """Block bounds (x0,z0,x1,z1) of the full finite world square (centred on the
    origin), always at least large enough to contain every generated chunk."""
    half = size // 2
    cx0, cz0, cx1, cz1 = -half, -half, -half + size - 1, -half + size - 1
    gx0, gz0, gx1, gz1 = _gen_chunk_bounds(world)
    cx0, cz0 = min(cx0, gx0), min(cz0, gz0)
    cx1, cz1 = max(cx1, gx1), max(cz1, gz1)
    return cx0 * 16, cz0 * 16, (cx1 + 1) * 16, (cz1 + 1) * 16


def render_topdown(world, size=54, shade=True):
    """Full-world surface map. Un-generated area = UNGEN; generated columns show
    the topmost non-air block, height-shaded. Returns (PIL.Image, ox, oz)."""
    x0, z0, x1, z1 = world_bounds(world, size)
    img = Image.new("RGB", (x1 - x0, z1 - z0), UNGEN)
    px = img.load()
    for wcx, wcz, ch in _overworld_chunks(world):
        bl = ch._blocks()
        bx, bz = wcx * 16 - x0, wcz * 16 - z0
        for lx in range(16):
            base = lx * 2048
            for lz in range(16):
                col = base + lz * 128
                top, bid = 0, 0
                for y in range(120, -1, -1):
                    b = bl[col + y]
                    if b != 0:
                        top, bid = y, b; break
                c = _color(bid)
                if c is None:
                    px[bx + lx, bz + lz] = SKY; continue
                if shade:
                    f = 0.55 + 0.45 * min(top, 96) / 96.0
                    c = (int(c[0] * f), int(c[1] * f), int(c[2] * f))
                px[bx + lx, bz + lz] = c
    return img, x0, z0


def render_slice(world, y, size=54):
    """Full-world horizontal cross-section at height y. Returns (PIL.Image, ox, oz)."""
    y = max(0, min(127, int(y)))
    x0, z0, x1, z1 = world_bounds(world, size)
    img = Image.new("RGB", (x1 - x0, z1 - z0), UNGEN)
    px = img.load()
    for wcx, wcz, ch in _overworld_chunks(world):
        bl = ch._blocks()
        bx, bz = wcx * 16 - x0, wcz * 16 - z0
        for lx in range(16):
            base = lx * 2048 + y
            for lz in range(16):
                b = bl[base + lz * 128]
                c = _color(b)
                px[bx + lx, bz + lz] = c if c is not None else (46, 46, 56)
    return img, x0, z0


def overlay(img, x0, z0, scale=1, spawn=None, player=None, entities=None,
            grid=False, boundary=False, size=54):
    """Scale the base map and draw overlays. Returns a display image.
      grid     -> faint 16-block chunk lines (bright every 8 chunks)
      boundary -> bright frame at the finite-world border (size chunks, on origin)
    """
    nw, nh = max(1, int(round(img.width * scale))), max(1, int(round(img.height * scale)))
    im = img.resize((nw, nh), Image.NEAREST) if (nw, nh) != (img.width, img.height) else img.copy()
    d = ImageDraw.Draw(im)

    def wp(wx, wz):
        return (int((wx - x0) * scale), int((wz - z0) * scale))

    if grid:
        step = max(6, int(round(16 * scale)))
        for gx in range(0, im.width + 1, step):
            heavy = ((gx // step) % 8) == 0
            d.line([(gx, 0), (gx, im.height)], fill=(90, 96, 108) if heavy else (60, 64, 74))
        for gy in range(0, im.height + 1, step):
            heavy = ((gy // step) % 8) == 0
            d.line([(0, gy), (im.width, gy)], fill=(90, 96, 108) if heavy else (60, 64, 74))
    if boundary:
        half = size // 2
        bx0, bz0 = wp(-half * 16, -half * 16)
        bx1, bz1 = wp((-half + size) * 16, (-half + size) * 16)
        d.rectangle([bx0, bz0, bx1 - 1, bz1 - 1], outline=(255, 170, 40), width=max(2, scale))
    if entities:
        for (ex, ez) in entities:
            x, y = wp(ex, ez)
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 80, 80))
    if spawn:
        x, y = wp(spawn[0], spawn[2])
        d.line([x - 5, y, x + 5, y], fill=(255, 255, 0), width=2)
        d.line([x, y - 5, x, y + 5], fill=(255, 255, 0), width=2)
    if player:
        x, y = wp(player[0], player[2])
        d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=(0, 220, 255), width=2)
    return im


# backwards-compat alias
overlay_markers = overlay
