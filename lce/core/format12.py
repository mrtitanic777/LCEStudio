"""Decoder for LCE chunk format version 0x000C ("Aquatic").

The old retail worlds (TU0 / Beta 1.6.6) store each chunk as a plain NBT
compound with a flat block ByteArray -- that is what lce.codec / lce.world read.
Newer title updates switched to a compact paletted "grid" format identified by a
version word at the very start of the (decompressed) chunk payload:

    0x0A  NBT format         (old -- handled by lce.world)
    0x0B  Elytra format
    0x0C  Aquatic format     <-- this module

An Aquatic chunk is 256 blocks tall.  Layout (all big-endian unless noted):

    [0x00] u16  version (0x000C)
    [0x02] i32  chunkX
    [0x06] i32  chunkZ
    [0x0a] i64  lastUpdate
    [0x12] i64  inhabitedTime
    [0x1a] u16  sectionSizeUnits          (bytes = units * 0x100)
    [0x1c] u16[16] section jump table     (byte offset of each Y-section's data)
    [0x3c] u8[16]  section size table     (0 => that section empty)
    [0x4c] ... section data ... light ... heightmap ... biomes ... NBT

Each non-empty section (16 blocks of Y) is 64 grids of 4x4x4.  The first 128
bytes of a section are the grid index table (64 * u16 little-endian):

    format = index >> 12            (top nibble)
    offset = (index & 0x0FFF) * 4   (from the end of the 128-byte table)

Grid formats (palette entries + position bitfield, optional trailing liquid
data we skip): 0=single, 2/3=2-block, 4/5=4-block, 6/7=8-block, 8/9=16-block,
0xE/0xF=full 64-block.  Blocks are 16-bit little-endian:

    waterlogged = v >> 15
    id          = (v >> 4) & 0x7FF
    data        = v & 0x0F

Credit: format documented by UtterEvergreen1 (Team-Lodestone LCE docs) and the
zugebot/LegacyEditor decoder; this is a clean-room Python reimplementation.
"""
import struct

import numpy as np

VERSION_NBT = 0x0A
VERSION_ELYTRA = 0x0B
VERSION_AQUATIC = 0x0C

# format nibble -> (palette_size, bits_per_block, has_liquid)
_GRID = {
    0x2: (2, 1, False), 0x3: (2, 1, True),
    0x4: (4, 2, False), 0x5: (4, 2, True),
    0x6: (8, 3, False), 0x7: (8, 3, True),
    0x8: (16, 4, False), 0x9: (16, 4, True),
}


def chunk_version(data):
    return struct.unpack_from(">H", data, 0)[0]


def _u16le(b, o):
    return b[o] | (b[o + 1] << 8)


def _decode_grid(data, gp, fmt):
    """Return 64 raw 16-bit block values for one 4x4x4 grid (YZX order)."""
    if fmt in (0xE, 0xF):                       # full: 64 * u16 LE
        return [_u16le(data, gp + 2 * i) for i in range(64)]
    psize, bits, _liquid = _GRID[fmt]
    palette = [_u16le(data, gp + 2 * i) for i in range(psize)]
    seg_base = gp + psize * 2
    segs = [int.from_bytes(data[seg_base + b * 8: seg_base + b * 8 + 8], "big")
            for b in range(bits)]
    out = []
    for i in range(64):
        idx = 0
        for b in range(bits):
            idx |= ((segs[b] >> (63 - i)) & 1) << b
        out.append(palette[idx])
    return out


def _encode_grid(vals):
    """One 4x4x4 grid (64 raw 16-bit values, p-order) -> (fmt, grid_bytes, single).
    Picks the smallest format that fits; grid_bytes is 4-byte-aligned."""
    distinct = sorted(set(vals))
    n = len(distinct)
    if n == 1 and vals[0] < 0x1000:                 # single, value stored in the index
        return 0, None, vals[0]                     # (needs top nibble 0 => id<256, no waterlog)
    for fmt, psize, bits in [(2, 2, 1), (4, 4, 2), (6, 8, 3), (8, 16, 4)]:
        if n <= psize:
            palette = distinct + [distinct[0]] * (psize - n)
            index_of = {v: i for i, v in enumerate(distinct)}
            segs = [0] * bits
            for i, v in enumerate(vals):
                idx = index_of[v]
                for b in range(bits):
                    if (idx >> b) & 1:
                        segs[b] |= 1 << (63 - i)
            gb = bytearray()
            for pv in palette:
                gb += struct.pack("<H", pv)
            for seg in segs:
                gb += seg.to_bytes(8, "big")
            return fmt, bytes(gb), None
    gb = bytearray()                                # full: 64 * u16 LE
    for v in vals:
        gb += struct.pack("<H", v)
    return 0xE, bytes(gb), None


def _encode_section(ids, data, wlog, s):
    """Encode Y-section s (16 y-layers) -> section bytes (128B index + grids), or
    None if the whole section is air."""
    y0 = s * 16
    headers = [0] * 64
    gdata = bytearray()
    nonair = False
    for gx in range(4):
        for gz in range(4):
            for gy in range(4):
                gidx = gx * 16 + gz * 4 + gy
                vals = []
                for p in range(64):
                    bx = p >> 4; bz = (p >> 2) & 3; by = p & 3
                    X, Y, Z = gx * 4 + bx, y0 + gy * 4 + by, gz * 4 + bz
                    v = (((1 if wlog[X, Y, Z] else 0) << 15)
                         | ((int(ids[X, Y, Z]) & 0x7FF) << 4) | (int(data[X, Y, Z]) & 0xF))
                    vals.append(v)
                if any(vals):
                    nonair = True
                fmt, gb, single = _encode_grid(vals)
                if fmt == 0:
                    headers[gidx] = single
                else:
                    headers[gidx] = (fmt << 12) | ((len(gdata) // 4) & 0xFFF)
                    gdata += gb
    if not nonair:
        return None
    sec = bytearray()
    for gidx in range(64):
        sec += struct.pack("<H", headers[gidx])
    sec += gdata
    return bytes(sec)


def encode_chunk(original, edits=None):
    """Re-encode a format-12 (Aquatic) chunk, applying `edits` = {(lx,y,lz):(id,meta)}
    (local chunk coords) on top of the ORIGINAL decoded grid — so every unedited
    block (incl. Aquatic-only ids and waterlogging) is preserved byte-for-byte in
    meaning; only the edited cells change. Header + light/heightmap/biomes/NBT tail
    are carried over from `original`. Returns the new chunk bytes."""
    dec = decode_chunk(original)
    ids = dec["ids"].copy(); data = dec["data"].copy(); wlog = dec["waterlogged"].copy()
    if edits:
        for (lx, y, lz), (bid, meta) in edits.items():
            if 0 <= lx < 16 and 0 <= y < 256 and 0 <= lz < 16:
                ids[lx, y, lz] = bid & 0x7FF
                data[lx, y, lz] = meta & 0xF
                wlog[lx, y, lz] = False
    su = struct.unpack_from(">H", original, 0x1a)[0]
    tail = original[0x4c + su * 0x100:]
    jump = [0] * 16; sizes = [0] * 16; secblob = bytearray()
    for s in range(16):
        sec = _encode_section(ids, data, wlog, s)
        if sec is None:
            continue
        padded = sec + b"\x00" * ((-len(sec)) % 0x100)
        jump[s] = len(secblob)
        sizes[s] = len(padded) // 0x100
        secblob += padded
    out = bytearray()
    out += struct.pack(">H", VERSION_AQUATIC)
    out += original[2:0x1a]                          # cx, cz, lastUpdate, inhabitedTime
    out += struct.pack(">H", len(secblob) // 0x100)  # sectionSizeUnits
    out += struct.pack(">16H", *jump)
    out += struct.pack(">16B", *sizes)
    out += secblob
    out += tail
    return bytes(out)


def decode_chunk(data):
    """Decode an Aquatic (0x0C) chunk payload.

    Returns dict: cx, cz, ids (np.uint16 [16,256,16] X,Y,Z), data (np.uint8
    same shape), waterlogged (bool array), and nbt_bytes (the trailing
    Entities/TileEntities/TileTicks NBT blob, or b'').
    """
    ver = chunk_version(data)
    if ver != VERSION_AQUATIC:
        raise ValueError("not an Aquatic (0x0C) chunk: version=0x%02x" % ver)
    cx = struct.unpack_from(">i", data, 2)[0]
    cz = struct.unpack_from(">i", data, 6)[0]

    o = 0x1a
    sec_units = struct.unpack_from(">H", data, o)[0]; o += 2
    sec_bytes = sec_units * 0x100
    jump = list(struct.unpack_from(">16H", data, o)); o += 32
    sizes = list(struct.unpack_from(">16B", data, o)); o += 16
    sec_data = 0x4c                             # == o

    ids = np.zeros((16, 256, 16), np.uint16)
    meta = np.zeros((16, 256, 16), np.uint8)
    wlog = np.zeros((16, 256, 16), np.bool_)

    for s in range(16):
        if sizes[s] == 0:
            continue
        base = sec_data + jump[s]               # 128-byte grid header
        gdata = base + 128
        for gx in range(4):
            for gz in range(4):
                for gy in range(4):
                    gidx = gx * 16 + gz * 4 + gy
                    v = _u16le(data, base + gidx * 2)
                    fmt = (v >> 12) & 0xF
                    if fmt == 0:                # single block (value in index)
                        blocks = None
                        single = v
                    else:
                        off = (v & 0x0FFF) * 4
                        blocks = _decode_grid(data, gdata + off, fmt)
                        single = None
                    ox, oy, oz = gx * 4, s * 16 + gy * 4, gz * 4
                    for p in range(64):
                        val = single if single is not None else blocks[p]
                        bx = p >> 4; bz = (p >> 2) & 3; by = p & 3
                        ids[ox + bx, oy + by, oz + bz] = (val >> 4) & 0x7FF
                        meta[ox + bx, oy + by, oz + bz] = val & 0xF
                        if val & 0x8000:
                            wlog[ox + bx, oy + by, oz + bz] = True

    # trailing sections: sec data (sec_bytes) then light/heightmap/biomes/NBT.
    # The NBT blob begins at the last top-level 0x0A 00 00 compound.
    tail = data.rfind(bytes([0x0a, 0x00, 0x00]))
    nbt_bytes = data[tail:] if tail >= 0 else b""
    return {"cx": cx, "cz": cz, "ids": ids, "data": meta,
            "waterlogged": wlog, "nbt": nbt_bytes}
