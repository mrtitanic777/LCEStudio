"""Structure copy / paste — move a box of blocks (with tile entities) between
LCE worlds, or save it to a portable .lstruct file.

    s = structure.copy_region(src_world, x1,y1,z1, x2,y2,z2)   # from one save
    structure.paste_region(dst_world, s, ax, ay, az)          # into another
    structure.save(s, "house.lstruct"); s = structure.load("house.lstruct")

Chests/signs/furnaces (tile entities) inside the box are carried and re-based to
the paste position, so their contents/text come along.
"""
import copy
import json
import struct as _struct

from . import nbt as N


def _data_nibble(chunk, x, y, z):
    d = chunk._data()
    if d is None:
        return 0
    i = y + (z & 15) * 128 + (x & 15) * 2048
    return (d[i >> 1] >> 4) if (i & 1) else (d[i >> 1] & 0x0F)


def copy_region(world, x1, y1, z1, x2, y2, z2, nether=False):
    """Snapshot a box: block ids + metadata + tile entities (relative coords)."""
    x1, x2 = sorted((x1, x2)); y1, y2 = sorted((max(0, y1), min(127, y2))); z1, z2 = sorted((z1, z2))
    dx, dy, dz = x2 - x1 + 1, y2 - y1 + 1, z2 - z1 + 1
    blocks = bytearray(dx * dy * dz)
    data = bytearray(dx * dy * dz)
    idx = 0
    for x in range(x1, x2 + 1):
        for y in range(y1, y2 + 1):
            for z in range(z1, z2 + 1):
                c = world.chunk(x >> 4, z >> 4, nether)
                if c is not None:
                    blocks[idx] = c.get_block(x, y, z)
                    data[idx] = _data_nibble(c, x, y, z)
                idx += 1
    tiles = []
    for wcx in range(x1 >> 4, (x2 >> 4) + 1):
        for wcz in range(z1 >> 4, (z2 >> 4) + 1):
            c = world.chunk(wcx, wcz, nether)
            if c is None or not c.tile_entities:
                continue
            for t in c.tile_entities.items:
                tx, ty, tz = t.get_value("x"), t.get_value("y"), t.get_value("z")
                if tx is None or not (x1 <= tx <= x2 and y1 <= ty <= y2 and z1 <= tz <= z2):
                    continue
                tt = copy.deepcopy(t)
                tt.set_value("x", tx - x1); tt.set_value("y", ty - y1); tt.set_value("z", tz - z1)
                tiles.append(tt)
    return {"dims": (dx, dy, dz), "blocks": bytes(blocks), "data": bytes(data), "tiles": tiles}


def paste_region(world, s, ax, ay, az, nether=False, skip_air=True, replace=True):
    """Stamp a snapshot at (ax,ay,az). Returns (blocks_written, tiles_written)."""
    dx, dy, dz = s["dims"]
    blocks, data = s["blocks"], s["data"]
    nb = 0; idx = 0
    for x in range(dx):
        for y in range(dy):
            for z in range(dz):
                b = blocks[idx]; d = data[idx]; idx += 1
                if skip_air and b == 0:
                    continue
                wx, wy, wz = ax + x, ay + y, az + z
                if not (0 <= wy < 128):
                    continue
                if world.chunk(wx >> 4, wz >> 4, nether) is None:
                    continue                                   # don't spill into ungenerated
                world.set_block(wx, wy, wz, b, d, nether=nether)
                nb += 1
    # tile entities -> offset and add
    nt = 0
    for t in s["tiles"]:
        rx, ry, rz = t.get_value("x"), t.get_value("y"), t.get_value("z")
        wx, wy, wz = ax + rx, ay + ry, az + rz
        c = world.chunk(wx >> 4, wz >> 4, nether)
        if c is None:
            continue
        tt = copy.deepcopy(t)
        tt.set_value("x", wx); tt.set_value("y", wy); tt.set_value("z", wz)
        if replace and c.tile_entities:
            c.tile_entities.items[:] = [e for e in c.tile_entities.items
                                        if (e.get_value("x"), e.get_value("y"), e.get_value("z"))
                                        != (wx, wy, wz)]
        c.add_tile_entity(tt); nt += 1
    return nb, nt


# ---------------------------------------------------------------- file format
_MAGIC = b"LSTRUCT1"


def save(s, path):
    """Portable structure file: header + blocks + data + NBT tile-entity list."""
    dx, dy, dz = s["dims"]
    tblob = bytearray()
    for t in s["tiles"]:
        b = N.serialize("", t, N.COMPOUND)
        tblob += _struct.pack(">I", len(b)) + b
    with open(path, "wb") as f:
        f.write(_MAGIC)
        f.write(_struct.pack(">iii", dx, dy, dz))
        f.write(_struct.pack(">I", len(s["blocks"]))); f.write(s["blocks"])
        f.write(s["data"])
        f.write(_struct.pack(">I", len(s["tiles"]))); f.write(bytes(tblob))
    return path


def load(path):
    d = open(path, "rb").read()
    assert d[:8] == _MAGIC, "not an .lstruct file"
    o = 8
    dx, dy, dz = _struct.unpack_from(">iii", d, o); o += 12
    (n,) = _struct.unpack_from(">I", d, o); o += 4
    blocks = d[o:o + n]; o += n
    data = d[o:o + n]; o += n
    (nt,) = _struct.unpack_from(">I", d, o); o += 4
    tiles = []
    for _ in range(nt):
        (ln,) = _struct.unpack_from(">I", d, o); o += 4
        _name, tag, _end = N.parse_tag(d[o:o + ln]); o += ln
        tiles.append(tag.value)
    return {"dims": (dx, dy, dz), "blocks": blocks, "data": data, "tiles": tiles}
