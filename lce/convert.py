"""Java Edition world  ->  LCE TU0 (Beta 1.6.6) console savegame.dat.

Pre-1.13 Java uses the SAME numeric block ids as TU0, so core terrain and
builds copy almost 1:1; blocks newer than Beta 1.6.6 (id > 96) are remapped to
the nearest classic block. Java chunks are 256 tall in 16-block Sections; TU0 is
a single 128-tall column, so only y 0-127 is carried.

We reuse a real TU0 save as a skeleton (its level.dat / player / Nether / map
files and VFS layout) and swap in freshly-built overworld regions, then write it
back through the proven console-exact path (inject.encode_chunk with the 5-byte
trailer + <=32KB guard, inject.build_retail with the 0xFF final-frame container).

    from lce import convert
    convert.convert(java_world_dir, template_bin, out_bin, level_name="MyWorld")
"""
import copy
import gzip
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

from . import nbt as N
from . import inject
import lce

WORLD_CHUNK = 27                      # LCE border: chunks -27..26 (54x54), origin-centred

# ids > 96 don't exist in Beta 1.6.6 -> nearest classic block (data dropped).
_REMAP = {
    97: 1, 98: 4, 99: 35, 100: 35, 101: 85, 102: 20, 103: 86, 104: 0, 105: 0,
    106: 18, 107: 85, 108: 67, 109: 67, 110: 2, 111: 0, 112: 45, 113: 85, 114: 67,
    115: 0, 116: 49, 117: 85, 118: 42, 119: 0, 120: 24, 121: 1, 122: 49, 123: 89,
    124: 89, 125: 5, 126: 44, 127: 0, 128: 67, 129: 15, 130: 54, 131: 0, 132: 0,
    133: 57, 134: 53, 135: 53, 136: 53, 137: 1, 138: 20, 139: 4, 140: 0, 141: 59,
    142: 59, 143: 0, 144: 0, 145: 42, 146: 54, 147: 70, 148: 70, 149: 93, 150: 93,
    151: 44, 152: 42, 153: 16, 154: 42, 155: 24, 156: 67, 157: 66, 158: 23, 159: 35,
    160: 20, 161: 18, 162: 17, 163: 53, 164: 53, 165: 35, 166: 0, 167: 96, 168: 1,
    169: 89, 170: 17, 171: 44, 172: 45, 173: 49, 174: 79, 175: 31,
}


def map_block(bid):
    if bid <= 96:
        return bid
    return _REMAP.get(bid, 4)          # unknown modern block -> cobblestone (keeps shape)


# 256-entry translation table (id -> Beta id) for fast whole-array remap
_BTAB = bytes(map_block(i) & 0xFF for i in range(256))


def _as_ubytes(lst, n):
    """A signed-int NBT byte array -> n unsigned bytes."""
    b = bytearray(n)
    for i in range(min(n, len(lst))):
        b[i] = lst[i] & 0xFF
    return b


def _remap_item(item):
    """Remap a single ItemStack's block-item id (Beta numeric) to a Beta id."""
    tg = item.get_tag("id") if hasattr(item, "get_tag") else None
    if tg is not None and isinstance(tg.value, int) and 0 <= tg.value < 256:
        tg.value = map_block(tg.value)


def _remap_item_holder(comp):
    """Remap item ids inside a tile-entity/entity: 'Items' list and/or 'Item'."""
    items = comp.get_value("Items")
    if items is not None:
        for it in items.items:
            _remap_item(it)
    single = comp.get_value("Item")             # dropped item / item frame
    if single is not None and hasattr(single, "get_tag"):
        _remap_item(single)


# ------------------------------------------------------------------ Java reader
def _decompress_chunk(sector):
    ln = struct.unpack_from(">I", sector, 0)[0]
    comp = sector[4]
    payload = sector[5:5 + ln - 1]
    if comp == 1:
        return gzip.decompress(payload)
    if comp == 2:
        return zlib.decompress(payload)
    raise ValueError("unknown chunk compression %d" % comp)


def _iter_region_raw(path):
    """Yield (cx, cz, decompressed_nbt_bytes) for every present chunk. Cheap:
    no NBT parse (that's left to the parallel workers)."""
    base = os.path.basename(path).split(".")
    rx, rz = int(base[1]), int(base[2])
    d = open(path, "rb").read()
    for i in range(1024):
        loc = struct.unpack_from(">I", d, i * 4)[0]
        off, cnt = loc >> 8, loc & 0xFF
        if off == 0:
            continue
        try:
            nbt = _decompress_chunk(d[off * 4096:(off + cnt) * 4096])
        except Exception:
            continue
        yield rx * 32 + (i % 32), rz * 32 + (i // 32), nbt


def read_java_region(path):
    """Yield (cx, cz, Level compound) for every present chunk in a Java region."""
    for cx, cz, nbt in _iter_region_raw(path):
        try:
            _name, tag, _ = N.parse_tag(nbt)
            level = tag.value.get_value("Level")
        except Exception:
            continue
        if level is not None:
            yield cx, cz, level


def _iter_alpha_chunks(world_dir):
    """Yield (cx, cz, decompressed_nbt_bytes) for a pre-McRegion ALPHA world
    (Infdev/Alpha/Beta 1.0-1.2): each chunk is a gzipped `c.<x>.<z>.dat` file in
    a base-36 folder tree. Same chunk NBT as McRegion, so it converts identically."""
    import glob
    for path in glob.glob(os.path.join(world_dir, "*", "*", "c.*.*.dat")):
        parts = os.path.basename(path)[:-4].split(".")     # ['c', x36, z36]
        if len(parts) < 3:
            continue
        try:
            cx, cz = int(parts[1], 36), int(parts[2], 36)
        except ValueError:
            continue
        try:
            raw = open(path, "rb").read()
            try:
                nbt = gzip.decompress(raw)
            except Exception:
                nbt = zlib.decompress(raw)
        except Exception:
            continue
        yield cx, cz, nbt


def _iter_world_chunks(java_world):
    """Auto-detect region (.mcr/.mca) vs Alpha (per-chunk) layout and yield
    (cx, cz, nbt_bytes, format_name)."""
    region_dir = os.path.join(java_world, "region")
    if os.path.isdir(region_dir) and any(
            f.startswith("r.") and f.endswith((".mcr", ".mca"))
            for f in os.listdir(region_dir)):
        for fn in sorted(os.listdir(region_dir)):
            if fn.startswith("r.") and fn.endswith((".mca", ".mcr")):
                for cx, cz, nbt in _iter_region_raw(os.path.join(region_dir, fn)):
                    yield cx, cz, nbt, "region"
    else:
        for cx, cz, nbt in _iter_alpha_chunks(java_world):
            yield cx, cz, nbt, "alpha"


def _nib(arr, i):
    return (arr[i >> 1] >> 4) & 0xF if (i & 1) else (arr[i >> 1] & 0xF)


def _set_nib(arr, i, v):
    if i & 1:
        arr[i >> 1] = (arr[i >> 1] & 0x0F) | ((v & 0xF) << 4)
    else:
        arr[i >> 1] = (arr[i >> 1] & 0xF0) | (v & 0xF)


# ------------------------------------------------------- Java chunk -> TU0 chunk
def java_to_tu0_chunk(cx, cz, level, copy_light=True):
    """Build a TU0 chunk NBT (root compound) from a Java Level compound."""
    blocks = bytearray(32768)
    data = bytearray(16384)
    skylight = bytearray(16384)
    blocklight = bytearray(16384)
    if copy_light:
        for i in range(16384):
            skylight[i] = 0xFF                      # default bright, overwritten below

    sections = level.get_value("Sections")
    mcr_blocks = level.get_value("Blocks")            # McRegion: 128-tall Blocks directly
    if sections is None and mcr_blocks is not None:
        # ---- McRegion (Beta) fast path: same YZX 128-tall layout as TU0 ----
        blocks = bytearray(_as_ubytes(mcr_blocks, 32768)).translate(_BTAB)
        jd = level.get_value("Data")
        if jd is not None:
            data = _as_ubytes(jd, 16384)
        if copy_light:
            jsl = level.get_value("SkyLight"); jbl = level.get_value("BlockLight")
            if jsl is not None:
                skylight = _as_ubytes(jsl, 16384)
            if jbl is not None:
                blocklight = _as_ubytes(jbl, 16384)
    elif sections is not None:
        for sec in sections.items:
            sy = sec.get_value("Y")
            if sy is None or sy > 7:                # y >= 128 doesn't fit TU0
                continue
            jb = sec.get_value("Blocks")            # 4096 (signed) ids
            jd = sec.get_value("Data")              # 2048 nibbles
            jadd = sec.get_value("Add")             # optional high-id nibbles
            jsl = sec.get_value("SkyLight")
            jbl = sec.get_value("BlockLight")
            if jb is None:
                continue
            for ly in range(16):
                gy = sy * 16 + ly
                if gy > 127:
                    continue
                for lz in range(16):
                    for lx in range(16):
                        li = (ly * 16 + lz) * 16 + lx
                        bid = jb[li] & 0xFF
                        if jadd is not None:
                            bid |= _nib(jadd, li) << 8
                        ti = gy + lz * 128 + lx * 2048
                        blocks[ti] = map_block(bid) & 0xFF
                        if jd is not None:
                            _set_nib(data, ti, _nib(jd, li))
                        if copy_light:
                            if jsl is not None:
                                _set_nib(skylight, ti, _nib(jsl, li))
                            if jbl is not None:
                                _set_nib(blocklight, ti, _nib(jbl, li))
    if not copy_light:
        skylight = bytearray(b"\xff" * 16384)       # uniform bright, tiny RLE

    # heightmap: reuse Java's if present, else recompute (top solid+1 per column)
    jhm = level.get_value("HeightMap")
    if jhm is not None and len(jhm) >= 256:
        heightmap = _as_ubytes(jhm, 256)
    else:
        heightmap = bytearray(256)
        for lx in range(16):
            for lz in range(16):
                top = 0
                for gy in range(127, -1, -1):
                    if blocks[gy + lz * 128 + lx * 2048]:
                        top = gy + 1
                        break
                heightmap[lz * 16 + lx] = min(top, 255)

    # tile entities + entities (Beta NBT == TU0). Deep-copy and remap block-item ids.
    tiles = []
    jte = level.get_value("TileEntities")
    if jte is not None:
        for t in jte.items:
            tt = copy.deepcopy(t)
            _remap_item_holder(tt)
            tiles.append(tt)
    ents = []
    jen = level.get_value("Entities")
    if jen is not None:
        for e in jen.items:
            ee = copy.deepcopy(e)
            _remap_item_holder(ee)
            ents.append(ee)

    root = N.Compound()
    lvl = N.Compound()
    lvl.set("Blocks", N.BYTE_ARRAY, list(blocks))
    lvl.set("Data", N.BYTE_ARRAY, list(data))
    lvl.set("SkyLight", N.BYTE_ARRAY, list(skylight))
    lvl.set("BlockLight", N.BYTE_ARRAY, list(blocklight))
    lvl.set("HeightMap", N.BYTE_ARRAY, list(heightmap))
    lvl.set("Entities", N.LIST, N.List(N.COMPOUND, ents))
    lvl.set("TileEntities", N.LIST, N.List(N.COMPOUND, tiles))
    lvl.set("LastUpdate", N.INT, 0)
    lvl.set("xPos", N.INT, cx)
    lvl.set("zPos", N.INT, cz)
    lvl.set("TerrainPopulated", N.INT, 1)
    root.set("Level", N.COMPOUND, lvl)
    return root


def _encode_chunk_safe(root):
    """Encode a chunk under TU0's 32KB single-frame limit. Degrade gracefully:
    as-is -> uniform light -> drop entities -> drop tile entities."""
    lvl = root.get_value("Level")
    for step in range(4):
        if step == 1:
            lvl.set("SkyLight", N.BYTE_ARRAY, [0xFF] * 16384)
            lvl.set("BlockLight", N.BYTE_ARRAY, [0] * 16384)
        elif step == 2:
            lvl.set("Entities", N.LIST, N.List(N.COMPOUND, []))
        elif step == 3:
            lvl.set("TileEntities", N.LIST, N.List(N.COMPOUND, []))
        try:
            return inject.encode_chunk(N.serialize("", root, N.COMPOUND))[0]
        except ValueError:
            continue
    raise ValueError("chunk %s,%s still >32KB after dropping light/entities/tiles"
                     % (lvl.get_value("xPos"), lvl.get_value("zPos")))


# --------------------------------------------------------------------- convert
def convert(java_world, template_bin, out_bin, box=864, spawn=None,
            level_name=None, copy_light=True, log=print):
    """Convert a Java world folder into a TU0 savegame.dat at out_bin.

    Auto-detects the source layout: region files (.mcr McRegion / .mca Anvil) or
    the pre-McRegion Alpha per-chunk format (Infdev/Alpha/Beta 1.0-1.2)."""
    # skeleton: real TU0 save (level.dat / player / Nether / maps / VFS layout)
    w = lce.World.open(template_bin)
    ents, filedata, ver = w._ents, dict(w._filedata), w._ver

    lim = WORLD_CHUNK
    # collect raw in-border chunks (cheap; parse happens in the workers)
    tasks = []
    skipped = 0
    fmt = None
    for cx, cz, nbt, fmt in _iter_world_chunks(java_world):
        if -lim <= cx < lim and -lim <= cz < lim:
            tasks.append((cx, cz, nbt))
        else:
            skipped += 1
    if not tasks:
        raise SystemExit("no Java chunks found in %s (region/ or Alpha layout)" % java_world)
    log("%s format: %d chunks to convert (parallel), %d outside border"
        % (fmt, len(tasks), skipped))

    def _work(task):
        cx, cz, nbt = task
        try:
            _n, tag, _ = N.parse_tag(nbt)
            level = tag.value.get_value("Level")
        except Exception:
            return None
        if level is None:
            return None
        root = java_to_tu0_chunk(cx, cz, level, copy_light=copy_light)
        try:
            return cx, cz, _encode_chunk_safe(root)
        except ValueError:
            return None                             # unconvertible chunk -> skip

    region_chunks = {}                              # (rx,rz) -> {(lcx,lcz): sector bytes}
    kept = 0
    with ThreadPoolExecutor(max_workers=(os.cpu_count() or 4)) as ex:
        for res in ex.map(_work, tasks):
            if res is None:
                continue
            cx, cz, sect = res
            region_chunks.setdefault((cx >> 5, cz >> 5), {})[(cx & 31, cz & 31)] = sect
            kept += 1
            if kept % 400 == 0:
                log("  encoded %d chunks..." % kept)
    log("chunks converted %d, skipped(outside border) %d" % (kept, skipped))

    # assemble each region file from its sector dict (empty base + place sectors)
    for (rx, rz), chunks in region_chunks.items():
        loc = bytearray(4096)
        ts = bytearray(4096)
        sectors = bytearray()
        sn = 2
        for (lcx, lcz), sect in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            n = len(sect) // 4096
            struct.pack_into(">I", loc, (lcx + lcz * 32) * 4, (sn << 8) | n)
            sectors += sect
            sn += n
        name = "r.%d.%d.mcr" % (rx, rz)
        filedata[name] = bytes(loc) + bytes(ts) + bytes(sectors)

    # ensure new region names are in the VFS order list (ents) with a timestamp
    have = {e[0] for e in ents}
    ts8 = b"\x00" * 8
    for (rx, rz) in region_chunks:
        name = "r.%d.%d.mcr" % (rx, rz)
        if name not in have:
            ents.append([name, 0, len(filedata[name]), ts8])

    # spawn + level name into the skeleton's level.dat
    jspawn, jseed = _read_java_level(java_world)
    lname, ltag, _ = N.parse_tag(filedata["level.dat"])
    ldata = ltag.value.get_value("Data") or ltag.value
    if spawn is None:
        spawn = jspawn
    if spawn:
        ldata.set("SpawnX", N.INT, int(spawn[0]))
        ldata.set("SpawnY", N.INT, int(spawn[1]) if len(spawn) > 2 else 64)
        ldata.set("SpawnZ", N.INT, int(spawn[-1]))
    if jseed is not None:                           # matching seed => matching biome tint
        ldata.set("RandomSeed", N.LONG, int(jseed))
    if level_name:
        ldata.set("LevelName", N.STRING, level_name)
    filedata["level.dat"] = N.serialize(lname, ltag.value, N.COMPOUND)

    data = inject.build_retail(ents, filedata, ver)
    if os.path.isdir(out_bin):
        out_bin = os.path.join(out_bin, "savegame.dat")
    with open(out_bin, "wb") as f:
        f.write(data)
    log("wrote %s (%d bytes)" % (out_bin, len(data)))
    return out_bin


def _read_java_level(java_world):
    """Return (spawn, seed) from a Java level.dat."""
    raw = open(os.path.join(java_world, "level.dat"), "rb").read()
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    _n, tag, _ = N.parse_tag(raw)
    data = tag.value.get_value("Data") or tag.value
    sx, sy, sz = data.get_value("SpawnX"), data.get_value("SpawnY"), data.get_value("SpawnZ")
    spawn = (sx, sy or 64, sz) if sx is not None else None
    return spawn, data.get_value("RandomSeed")


# ============================================ reverse: LCE (console) -> Java
def _write_java_region(chunks, path):
    """Write a Java McRegion `.mcr` from {(lcx,lcz): chunk_nbt_bytes} -- each chunk
    zlib-compressed (Java's scheme) instead of LZX (Xbox). Every present chunk gets
    a real (non-zero) timestamp; a zero timestamp makes some Minecraft builds treat
    the chunk as absent and regenerate it from the seed."""
    import time as _time
    ts_val = int(_time.time())
    loc = bytearray(4096)
    ts = bytearray(4096)
    sectors = bytearray()
    sn = 2
    for (lcx, lcz), nbt in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        comp = zlib.compress(nbt, 9)
        body = struct.pack(">I", len(comp) + 1) + b"\x02" + comp   # length, 2 = zlib
        body += b"\x00" * ((-len(body)) % 4096)
        n = len(body) // 4096
        idx = (lcx + lcz * 32) * 4
        struct.pack_into(">I", loc, idx, (sn << 8) | n)
        struct.pack_into(">I", ts, idx, ts_val)                    # non-zero timestamp
        sectors += body
        sn += n
    with open(path, "wb") as f:
        f.write(bytes(loc) + bytes(ts) + bytes(sectors))


# blocks a standing player passes through (air + plants/torches/thin decor) -- not
# "ground" for the purpose of finding a safe surface to stand on.
_PASSABLE = frozenset({0, 6, 8, 9, 10, 11, 27, 28, 30, 31, 32, 37, 38, 39, 40, 50,
                       51, 55, 59, 63, 65, 66, 68, 69, 70, 72, 75, 76, 77, 78, 83, 90})


def _surface_y(w, x, z, nether=False):
    """Highest solid (non-passable) block at (x,z) + 1 = the Y where a player stands
    safely on the surface. Returns None if the column has no solid ground."""
    for y in range(126, -1, -1):
        if w.get_block(x, y, z, nether) not in _PASSABLE:
            return y + 1
    return None


def _dig_out_y(w, x, feet_y, z, nether=False):
    """If the player's feet block is inside solid terrain, raise them to the first
    air block above -- but ONLY as far as the first opening, so an indoor player is
    never teleported up onto the roof. Leaves an already-clear position untouched."""
    y = feet_y
    while y < 127 and w.get_block(x, y, z, nether) not in _PASSABLE:
        y += 1
    return y


def _write_java_level(w, out_dir, level_name):
    ln, ltag, _ = N.parse_tag(w._filedata["level.dat"])
    root = ltag.value
    data = root.get_value("Data") or root
    data.set("version", N.INT, 19132)                   # McRegion world version
    if level_name:
        data.set("LevelName", N.STRING, level_name)
    player_here = None                                   # (floor x, y, floor z) of the player
    try:                                                # Java Beta keeps the player in level.dat
        _pn, ptag, _ = N.parse_tag(w._filedata[w._pkey])
        player = ptag.value
        pos = player.get_value("Pos")                   # List of [x,y,z] doubles
        if pos is not None and len(pos) == 3:
            px, py, pz = pos[0], pos[1], pos[2]
            in_nether = (player.get_value("Dimension") == -1)
            fx, fz = int(px // 1), int(pz // 1)
            safe = _dig_out_y(w, fx, int(py // 1), fz, in_nether)  # only lift if buried
            if safe > int(py // 1):                     # feet were inside terrain -> lift out
                py = float(safe)
                pos.items[1] = py
                player.set("OnGround", N.BYTE, 1)
            if not in_nether:                            # remember overworld standing spot
                player_here = (fx, int(py // 1), fz)
        data.set("Player", N.COMPOUND, player)
    except Exception:
        pass
    # Console Minecraft (LCE) loads you at your LAST position, not the world spawn.
    # Java Beta can drop you at the world spawn point instead -- which in many worlds
    # is far from the player's base (empty terrain), so the world "looks" fresh/empty
    # and the builds "missing". Anchor the world spawn to the player's last standing
    # spot so you load in at your base, matching the console. Fall back to snapping
    # the stored SpawnY onto the real surface when there's no player.
    if player_here is not None:
        data.set("SpawnX", N.INT, player_here[0])
        data.set("SpawnY", N.INT, player_here[1])
        data.set("SpawnZ", N.INT, player_here[2])
    else:
        sx, sz = data.get_value("SpawnX"), data.get_value("SpawnZ")
        if sx is not None and sz is not None:
            sy = _surface_y(w, int(sx), int(sz))
            if sy is not None:
                data.set("SpawnY", N.INT, sy)
    with open(os.path.join(out_dir, "level.dat"), "wb") as f:
        f.write(gzip.compress(N.serialize(ln, root, N.COMPOUND)))


def _to_java_dim(w, dim_dir, region_list, nether):
    region_dir = os.path.join(dim_dir, "region")
    os.makedirs(region_dir, exist_ok=True)
    total = 0
    for rx, rz in region_list:
        reg = w.region(rx, rz, nether)
        reg.decode_all()
        chunks = {(lcx, lcz): c.serialize() for (lcx, lcz), c in reg.chunks.items()}
        total += len(chunks)
        if chunks:
            _write_java_region(chunks, os.path.join(region_dir, "r.%d.%d.mcr" % (rx, rz)))
    return total


# level.dat keys added AFTER Beta 1.6.6 (TU0) — a TU0 loader predates them, so a
# TU1-TU5 world is downgraded by resetting the container version + stripping these.
_POST_TU0_LEVEL_KEYS = ("MapFeatures", "generatorName", "generatorVersion", "GameType",
                        "spawnBonusChest", "hasBeenInCreative", "newSeaLevel",
                        "allowCommands", "initialized", "DataVersion")


# entities that exist in TU0 (Beta 1.6.6). Anything else (Enderman/CaveSpider/
# Silverfish/XPOrb/… added in Beta 1.8+) crashes a TU0 world load and is removed.
_TU0_ENTITIES = frozenset({
    "Creeper", "Skeleton", "Spider", "Giant", "Zombie", "Slime", "Ghast",
    "PigZombie", "Pig", "Sheep", "Cow", "Chicken", "Squid", "Wolf", "Mob", "Monster",
    "Item", "Painting", "Arrow", "Snowball", "PrimedTnt", "FallingSand",
    "Minecart", "Boat", "Egg",
})
# tile-entity types TU0 (Beta 1.6.6) knows. ANY other id (EnderChest=1.3, Skull=1.4,
# Beacon/Cauldron/EnchantTable/Hopper/Comparator/Banner/…) CRASHES a TU0 world the
# moment it loads a chunk holding one -- the downgrade must strip them. (The post-TU0
# BLOCK under them is already remapped by _TU0_BLOCK_REMAP; only the ghost data remains.)
_TU0_TILE_ENTITIES = frozenset({
    "Chest", "Furnace", "Sign", "MobSpawner", "Trap", "RecordPlayer", "Music",
})
_TU0_MAX_BLOCK = 96                      # Beta 1.6.6 tops out at trapdoor (96)
# post-1.6.6 block ids (also used as item ids) -> nearest TU0 equivalent
_TU0_BLOCK_REMAP = {101: 85, 102: 20, 103: 86, 104: 6, 105: 6, 106: 31, 107: 85,
                    108: 67, 109: 67, 110: 3, 111: 37, 112: 87, 113: 85, 114: 67,
                    115: 87, 116: 47, 117: 61, 118: 42, 120: 49, 121: 24, 122: 49}

# blocks that were added AFTER Beta 1.6.6 but still have an id <= 96, so the ">96"
# remap misses them and TU0 FREEZES when one loads near you. Pistons are Beta 1.7
# (29 sticky, 33 piston, 34 head, 36 the technical moving block); 95 is stained glass
# in later editions (unused in 1.6.6). -> nearest TU0 block (heads/moving -> air/cobble).
_TU0_INVALID_LOW = {29: 4, 33: 4, 34: 4, 36: 0, 95: 20}


_TU0_ITEM_FALLBACK = 260        # apple: valid, edible, stackable TU0 placeholder for any post-TU0 item


def _remap_item_id(bid):
    """Map any item/block id to a TU0 (Beta 1.6.6) equivalent.
      * block 0..96                     -> unchanged
      * block 97..255 (post-TU0 block)  -> nearest TU0 block (_TU0_BLOCK_REMAP, else cobble 4)
      * pure item 256..358              -> unchanged (Beta 1.6.6 items, tools..bed, cookie, map)
      * music discs 2256/2257           -> unchanged (gold '13' & green 'cat' exist since Alpha)
      * pure item 359+ (post-TU0)       -> apple 260. Covers Beta 1.7 shears (359), the whole
        Beta 1.8 Adventure-Update food block (melon 360, seeds 361-362, meats 363-366, rotten
        flesh 367, …), 1.0 nether/potion items, spawn eggs (383), and the later discs (2258+).
        These are the 'TU5 items such as food' that a TU0 world can't instantiate."""
    bid = int(bid)
    if bid < 0:
        return bid
    if bid <= _TU0_MAX_BLOCK:
        return bid
    if bid < 256:
        return _TU0_BLOCK_REMAP.get(bid, 4)
    if 256 <= bid <= 358 or bid in (2256, 2257):
        return bid
    return _TU0_ITEM_FALLBACK


# Aquatic (container v11+) saves store some item ids as flattened STRINGS
# ("minecraft:filled_map") instead of the classic numeric id. TU0 wants numeric
# Shorts, so map the flattened name -> Beta-1.6.6 numeric id. Anything not here (a
# genuinely post-TU0 item) falls back to apple in _remap_item_id, so the table only
# needs the ids TU0 actually has.
_ITEM_NAME_TO_ID = {
    # blocks (also valid as items)
    "air": 0, "stone": 1, "grass_block": 2, "grass": 2, "dirt": 3, "cobblestone": 4,
    "oak_planks": 5, "planks": 5, "sapling": 6, "oak_sapling": 6, "bedrock": 7,
    "sand": 12, "gravel": 13, "gold_ore": 14, "iron_ore": 15, "coal_ore": 16,
    "oak_log": 17, "log": 17, "oak_leaves": 18, "leaves": 18, "sponge": 19,
    "glass": 20, "lapis_ore": 21, "lapis_block": 22, "dispenser": 23, "sandstone": 24,
    "note_block": 25, "noteblock": 25, "powered_rail": 27, "golden_rail": 27,
    "detector_rail": 28, "cobweb": 30, "web": 30, "tall_grass": 31, "dead_bush": 32,
    "white_wool": 35, "wool": 35, "dandelion": 37, "yellow_flower": 37, "poppy": 38,
    "red_flower": 38, "rose": 38, "brown_mushroom": 39, "red_mushroom": 40,
    "gold_block": 41, "iron_block": 42, "smooth_stone_slab": 44, "stone_slab": 44,
    "slab": 44, "bricks": 45, "brick_block": 45, "tnt": 46, "bookshelf": 47,
    "mossy_cobblestone": 48, "obsidian": 49, "torch": 50, "spawner": 52,
    "mob_spawner": 52, "oak_stairs": 53, "chest": 54, "diamond_ore": 56,
    "diamond_block": 57, "crafting_table": 58, "furnace": 61, "ladder": 65, "rail": 66,
    "cobblestone_stairs": 67, "lever": 69, "stone_pressure_plate": 70,
    "iron_door_block": 71, "redstone_ore": 73, "stone_button": 77, "snow": 78,
    "snow_layer": 78, "ice": 79, "snow_block": 80, "cactus": 81, "clay": 82,
    "jukebox": 84, "oak_fence": 85, "fence": 85, "pumpkin": 86, "netherrack": 87,
    "soul_sand": 88, "glowstone": 89, "carved_pumpkin": 91, "jack_o_lantern": 91,
    "cake": 92, "oak_trapdoor": 96, "trapdoor": 96,
    # pure items
    "iron_shovel": 256, "iron_pickaxe": 257, "iron_axe": 258, "flint_and_steel": 259,
    "apple": 260, "bow": 261, "arrow": 262, "coal": 263, "charcoal": 263,
    "diamond": 264, "iron_ingot": 265, "gold_ingot": 266, "iron_sword": 267,
    "wooden_sword": 268, "wooden_shovel": 269, "wooden_pickaxe": 270, "wooden_axe": 271,
    "stone_sword": 272, "stone_shovel": 273, "stone_pickaxe": 274, "stone_axe": 275,
    "diamond_sword": 276, "diamond_shovel": 277, "diamond_pickaxe": 278,
    "diamond_axe": 279, "stick": 280, "bowl": 281, "mushroom_stew": 282,
    "golden_sword": 283, "golden_shovel": 284, "golden_pickaxe": 285, "golden_axe": 286,
    "string": 287, "feather": 288, "gunpowder": 289, "wooden_hoe": 290, "stone_hoe": 291,
    "iron_hoe": 292, "diamond_hoe": 293, "golden_hoe": 294, "wheat_seeds": 295,
    "seeds": 295, "wheat": 296, "bread": 297, "leather_helmet": 298,
    "leather_chestplate": 299, "leather_leggings": 300, "leather_boots": 301,
    "chainmail_helmet": 302, "chainmail_chestplate": 303, "chainmail_leggings": 304,
    "chainmail_boots": 305, "iron_helmet": 306, "iron_chestplate": 307,
    "iron_leggings": 308, "iron_boots": 309, "diamond_helmet": 310,
    "diamond_chestplate": 311, "diamond_leggings": 312, "diamond_boots": 313,
    "golden_helmet": 314, "golden_chestplate": 315, "golden_leggings": 316,
    "golden_boots": 317, "flint": 318, "porkchop": 319, "cooked_porkchop": 320,
    "painting": 321, "golden_apple": 322, "sign": 323, "oak_sign": 323,
    "wooden_door": 324, "oak_door": 324, "bucket": 325, "water_bucket": 326,
    "lava_bucket": 327, "minecart": 328, "saddle": 329, "iron_door": 330,
    "redstone": 331, "snowball": 332, "boat": 333, "oak_boat": 333, "leather": 334,
    "milk_bucket": 335, "brick": 336, "clay_ball": 337, "sugar_cane": 338, "reeds": 338,
    "paper": 339, "book": 340, "slime_ball": 341, "chest_minecart": 342,
    "furnace_minecart": 343, "egg": 344, "compass": 345, "fishing_rod": 346,
    "clock": 347, "glowstone_dust": 348, "cod": 349, "fish": 349, "cooked_cod": 350,
    "cooked_fish": 350, "dye": 351, "ink_sac": 351, "bone": 352, "sugar": 353,
    "cake_item": 354, "bed": 355, "white_bed": 355, "repeater": 356, "cookie": 357,
    "map": 358, "filled_map": 358,
}


def _item_num_id(v):
    """An item 'id' value (numeric OR a flattened 'minecraft:name' string) -> a numeric
    id. Unknown names resolve via the fallback path in _remap_item_id."""
    if isinstance(v, str):
        name = v.split(":")[-1].strip().lower()
        return _ITEM_NAME_TO_ID.get(name, 4096)     # 4096 -> _remap_item_id fallback
    return int(v)


def _remap_item_stack(it, stats):
    """Resolve one ItemStack's id (numeric or Aquatic string), remap it to a TU0-valid
    numeric id, and store it back as a Short (TU0 never uses string ids). Returns True
    if the stack changed."""
    t = it.get_tag("id")
    if t is None:
        return False
    orig = t.value
    nid = _remap_item_id(_item_num_id(orig))
    if isinstance(orig, str) or nid != orig or t.id != N.SHORT:
        it.set("id", N.SHORT, nid); stats["items_remapped"] += 1
        return True
    return False


def _remap_stack_list(taglist_value, stats):
    """Remap every ItemStack id in a taglist value (a tile-entity/minecart 'Items' list).
    Returns True if anything changed."""
    if taglist_value is None:
        return False
    changed = False
    for it in getattr(taglist_value, "items", ()):
        if _remap_item_stack(it, stats):
            changed = True
    return changed


def _remap_tu0_holder(comp, stats):
    """Remap post-TU0 item ids inside any container/holder compound: its 'Items' list
    (chest, dispenser=Trap, furnace, chest-minecart) and/or a single 'Item' stack
    (dropped Item entity, item frame). Returns True if anything changed."""
    changed = _remap_stack_list(comp.get_value("Items"), stats)
    single = comp.get_value("Item")
    if single is not None and hasattr(single, "get_tag"):
        if _remap_item_stack(single, stats):
            changed = True
    return changed


# player-NBT keys that do NOT exist in Beta 1.6.6. A TU0 world FREEZES at spawn-in
# when a profile carries these (hunger = Beta 1.8, XP = Beta 1.9/1.0, GamePrivileges
# = a later-TU LCE feature). Removing them is safe: TU0 fills any absent tag with a
# default (full health, etc.). The clean profile in a mixed save loads; the tagged
# ones freeze -- so strip every profile down to the TU0 baseline.
_POST_TU0_PLAYER_KEYS = (
    "foodLevel", "foodTickTimer", "foodSaturationLevel", "foodExhaustionLevel",
    "Xp", "XpP", "XpLevel", "XpTotal", "XpSeed",
    "GamePrivileges", "SelectedItemSlot", "playerGameType", "PlayerGameType",
    "abilities", "Abilities", "EnderItems", "ActiveEffects", "Attributes",
    "HealF", "AbsorptionAmount", "Invulnerable", "PortalCooldown",
    "SpawnForced", "seenCredits", "recipeBook", "UUID", "UUIDLeast", "UUIDMost",
    "DataVersion",
)


def _strip_player_keys(comp, stats):
    """Remove post-TU0 tags from one player compound (NBT value dict). Returns True
    if anything changed."""
    changed = False
    for k in _POST_TU0_PLAYER_KEYS:
        if comp.get_tag(k) is not None:
            comp.remove(k); stats["player_keys"] += 1; changed = True
    return changed


# The EXACT level.dat key set a genuine TU0 world carries (verified against a real
# TU0 save). A whitelist beats a blacklist here: every later TU adds its own keys
# (TU5 adds GameType/MapFeatures/generator*/Stronghold*/hardcore/…; Aquatic adds
# many more), and any key TU0's loader doesn't expect can destabilise the load.
# Keep only these; strip everything else regardless of which TU produced the save.
_TU0_LEVEL_KEYS = frozenset({
    "LastPlayed", "LevelName", "RandomSeed", "SizeOnDisk",
    "SpawnX", "SpawnY", "SpawnZ", "Time",
    "rainTime", "raining", "thunderTime", "thundering", "version",
})
_TU0_LEVEL_VERSION = 19132       # Beta McRegion level.dat version stamp


def _remap_block_id(bid):
    """Map any placed-block id to a TU0 (Beta 1.6.6) block. Unlike _remap_item_id
    (which preserves pure items 256+), EVERYTHING here is a world block: ids 0..96
    pass through, 97..255 go to the nearest TU0 block, and anything >=256 (the newer
    11-bit block ids Aquatic can carry) falls back to cobblestone."""
    bid = int(bid)
    if bid <= _TU0_MAX_BLOCK:
        return bid
    if bid < 256:
        return _TU0_BLOCK_REMAP.get(bid, 4)
    return 4


def _block_lut():
    """2048-entry uint8 LUT: 11-bit Aquatic block id -> TU0 block id (for vectorized
    transcode). Built once, cached on the function."""
    import numpy as np
    lut = getattr(_block_lut, "_c", None)
    if lut is None:
        lut = np.full(2048, 4, np.uint8)                 # >=256 -> cobblestone
        lut[:97] = np.arange(97, dtype=np.uint8)         # 0..96 pass through
        for b in range(97, 256):
            lut[b] = _TU0_BLOCK_REMAP.get(b, 4)          # 97..255 -> nearest TU0
        _block_lut._c = lut
    return lut


def _safe_dynamic_nbt(raw):
    """Find the entity NBT compound in a raw format-12 chunk and return its EXACT bytes,
    validated by a real parse. format12.decode_chunk locates it with a naive
    rfind(0a0000) that on some chunks grabs a garbage offset (splicing that in makes the
    whole legacy chunk unparseable, so the chunk was being dropped). Scan every 0a0000,
    prefer the first compound that actually holds Entities/TileEntities, and fall back to
    b'' (empty entities) rather than ever returning malformed bytes -- so the chunk's
    BLOCKS always survive even if its entities can't be recovered."""
    best = b""
    i = 0
    while True:
        p = raw.find(b"\x0a\x00\x00", i)
        if p < 0:
            return best
        try:
            _n, tag, off = N.parse_tag(raw[p:])
            comp = tag.value
            exact = raw[p:p + off]
            if hasattr(comp, "get_tag") and (comp.get_tag("Entities") is not None
                                             or comp.get_tag("TileEntities") is not None):
                return exact                     # the real entity compound
            if not best:
                best = exact                     # parseable fallback (usually empty)
        except Exception:
            pass
        i = p + 1


def _aquatic_chunk_to_legacy(raw):
    """Transcode one Aquatic (format-12, 256-tall paletted) chunk into TU0 legacy
    128-tall McRegion NBT bytes. The bottom 128 layers are kept (TU0's height cap),
    block ids are remapped to TU0 blocks, and the trailing Entities/TileEntities NBT
    is spliced in (later sanitized like any other chunk). Returns NBT bytes ready
    for N.parse_tag -> Chunk."""
    import numpy as np
    from . import format12
    d = format12.decode_chunk(raw)
    ids, meta = d["ids"], d["data"]              # np [16,256,16] X,Y,Z
    # bottom 128 layers, reordered to YZX flat (idx = y + z*128 + x*2048) and remapped
    sub = ids[:, :128, :].transpose(0, 2, 1)     # [x, z, y]
    blocks = _block_lut()[sub].reshape(-1).tobytes()
    mflat = (meta[:, :128, :].transpose(0, 2, 1).astype(np.uint8) & 0x0F).reshape(-1)
    dnib = (mflat[0::2] | (mflat[1::2] << 4)).astype(np.uint8).tobytes()
    c = dict(xPos=d["cx"], zPos=d["cz"], LastUpdate=0, InhabitedTime=0,
             Blocks=blocks, Data=dnib,
             SkyLight=b"\xff" * 16384, BlockLight=b"\x00" * 16384,
             HeightMap=b"\x00" * 256, TerrainPopulatedFlags=1,
             Biomes=b"\x01" * 256, dynamic=_safe_dynamic_nbt(raw))
    from . import codec
    return codec.build_legacy_chunk_nbt(c)


def downgrade_to_tu0(world, target_ver=2, sanitize=True, full_rewrite=None, log=print):
    """Make a TU1-TU5 (old-NBT) world loadable on TU0. The chunk FORMAT is already
    TU0-compatible, so builds are preserved. Two things change:
      * SAVE version markers — container reset to `target_ver` (2), and the
        level.dat reduced to the exact TU0 key set (everything a later TU added —
        GameType/MapFeatures/generator*/Stronghold*/hardcore/DataVersion/… — stripped);
      * CHUNK FORMAT (any TU) — old-NBT chunks (TU0-TU5) are already TU0-native;
        compressed-storage chunks (TU9-~TU54) are decoded to 128-tall old-NBT by the
        loader and (with full_rewrite) re-serialized as legacy; Aquatic format-12
        chunks (TU69+) are transcoded to legacy old-NBT here. Blocks above y127 are
        dropped (TU0's 128 height cap);
      * CONTENT (if sanitize) — entities TU0 can't instantiate (Enderman, …) are
        removed from chunks, placed blocks > 96 and post-TU0 items (incl. container
        and dropped items) are remapped to TU0 equivalents, and post-TU0 player tags
        are stripped from every profile. Post-TU0 content is what crashes a TU0 load.

    full_rewrite: re-serialize EVERY chunk to legacy old-NBT (needed to fully convert
    a compressed-storage source, whose untouched chunks would otherwise stay in the
    v8/v9/v10 format TU0 can't read). Default (None) auto-enables it when the source
    isn't already pure old-NBT. Returns a stats dict."""
    import numpy as np
    src_ver = world._ver                         # container version of the SOURCE TU
    world._ver = target_ver
    data = world.level
    stats = {"level_keys": [], "entities_removed": 0, "entity_types": set(),
             "blocks_remapped": 0, "items_remapped": 0, "player_keys": 0,
             "players_cleaned": 0, "aquatic_converted": 0, "chunks_rewritten": 0,
             "chunk_errors": 0, "tile_entities_removed": 0, "te_types": set(),
             "src_ver": src_ver}
    for k in [k for k in data.keys() if k not in _TU0_LEVEL_KEYS]:   # whitelist strip
        data.remove(k); stats["level_keys"].append(k)
    vt = data.get_tag("version")
    if vt is not None:
        vt.value = _TU0_LEVEL_VERSION
    world._dirty_level = True

    if sanitize:
        for name in [n for n in world._filedata if n.endswith(".mcr")]:
            nether = name.startswith("DIM-1")
            rx, rz = (int(v) for v in name[:-4].split("/")[-1].split(".")[1:3])
            reg = world.region(rx, rz, nether)
            reg.decode_all()                         # whole-region scan: force-decode
            # transcode Aquatic (format-12) chunks -> legacy old-NBT Chunk objects
            had_aquatic = bool(getattr(reg, "aquatic_raw", None))
            if had_aquatic:
                for (lcx, lcz), raw in list(reg.aquatic_raw.items()):
                    try:
                        nbt = _aquatic_chunk_to_legacy(raw)
                        nm, tag, _ = N.parse_tag(nbt)
                        from .world import Chunk
                        reg.chunks[(lcx, lcz)] = Chunk(reg, lcx, lcz, nm, tag.value)
                        stats["aquatic_converted"] += 1
                    except Exception:
                        stats["chunk_errors"] += 1
                reg.aquatic_raw.clear()
            # decide whether this region must be fully re-serialized to legacy: yes if
            # the caller forced it, if we just transcoded Aquatic chunks into it, or
            # (auto) if the region actually holds v8/v9/v10 compressed-storage chunks
            # that TU0 can't read. Native old-NBT regions are left byte-intact except
            # for the chunks that content-sanitize changes -- never rewritten wholesale.
            if full_rewrite is not None:
                do_full = full_rewrite
            elif had_aquatic:
                do_full = True
            else:
                from . import codec
                do_full = codec.region_has_compressed(reg._raw)
            for (lcx, lcz), ch in reg.chunks.items():
                changed = do_full
                ents = ch.entities
                if ents and ents.items:
                    bad = [e for e in ents.items if e.get_value("id") not in _TU0_ENTITIES]
                    if bad:
                        for e in bad:
                            stats["entity_types"].add(e.get_value("id"))
                        stats["entities_removed"] += len(bad)
                        ents.items[:] = [e for e in ents.items if e.get_value("id") in _TU0_ENTITIES]
                        changed = True
                # remap post-TU0 items held inside surviving entities (chest-minecart
                # 'Items', dropped-Item / item-frame 'Item')
                if ents and ents.items:
                    for e in ents.items:
                        if _remap_tu0_holder(e, stats):
                            changed = True
                # block remap (post-96 block -> nearest TU0 block) FIRST, so the
                # tile-entity orphan check below sees the final blocks
                bl = ch._blocks()                       # list[int] 32768
                arr = np.frombuffer(bytes(bytearray(bl)), np.uint8)
                a2 = arr.copy()
                for bid in np.unique(arr[arr > _TU0_MAX_BLOCK]):     # post-96 blocks
                    a2[arr == bid] = _TU0_BLOCK_REMAP.get(int(bid), 4)
                for bid, repl in _TU0_INVALID_LOW.items():           # post-1.6.6 low-id blocks (pistons…)
                    a2[arr == bid] = repl
                if not np.array_equal(a2, arr):
                    bl[:] = a2.tolist(); stats["blocks_remapped"] += int((a2 != arr).sum()); changed = True
                # sanitize tile-entities: drop post-TU0 TYPES (EnderChest/Skull/Beacon/…
                # crash TU0 on load), out-of-range positions (y not in 0..127, e.g. from a
                # dropped 256-tall upper section), and any orphaned by the block remap; then
                # remap post-TU0 items inside the survivors (chest/dispenser/furnace).
                tes = ch.tile_entities
                if tes and tes.items:
                    from .world import _te_orphaned
                    kept = []
                    for te in tes.items:
                        tid = te.get_value("id")
                        tx, ty, tz = te.get_value("x"), te.get_value("y"), te.get_value("z")
                        in_range = ty is not None and 0 <= ty < 128 and tx is not None and tz is not None
                        orphan = in_range and _te_orphaned(tid, ch.get_block(tx, ty, tz))
                        if tid not in _TU0_TILE_ENTITIES or not in_range or orphan:
                            stats["tile_entities_removed"] += 1
                            stats["te_types"].add(tid)
                            changed = True; continue
                        kept.append(te)
                    if len(kept) != len(tes.items):
                        tes.items[:] = kept
                    for te in tes.items:
                        if _remap_tu0_holder(te, stats):
                            changed = True
                if changed:
                    ch._mark(); stats["chunks_rewritten"] += 1

        for pk in world.players():                      # every profile, in place
            pn, ptag, _ = N.parse_tag(world._filedata[pk])
            pchanged = _strip_player_keys(ptag.value, stats)  # drop post-TU0 player tags
            if pchanged:
                stats["players_cleaned"] += 1
            inv = ptag.value.get_tag("Inventory")
            if inv is not None:
                for it in inv.value.items:
                    if _remap_item_stack(it, stats):    # handles numeric + Aquatic string ids
                        pchanged = True
            if pchanged:                                # re-serialize this profile directly
                world._filedata[pk] = N.serialize(pn, ptag.value, N.COMPOUND)

    log("downgraded to TU0 (container v%d -> v%d): stripped %d level keys, "
        "converted %d Aquatic chunks (%d errors), rewrote %d chunks, removed %d entities %s, "
        "removed %d post-TU0 tile-entities %s, remapped %d blocks + %d items, "
        "cleaned %d profiles (%d post-TU0 player tags)"
        % (src_ver, target_ver, len(stats["level_keys"]), stats["aquatic_converted"],
           stats["chunk_errors"], stats["chunks_rewritten"], stats["entities_removed"],
           sorted(stats["entity_types"]) or "", stats["tile_entities_removed"],
           sorted(t for t in stats["te_types"] if t) or "", stats["blocks_remapped"],
           stats["items_remapped"], stats["players_cleaned"], stats["player_keys"]))
    return stats


def to_java(lce_save, out_dir, level_name=None, log=print, owner=None):
    """Convert an LCE (Xbox 360 TU0) save into a COMPLETE Java Beta world folder:
    level.dat (+ level.dat_old), session.lock, icon.png, region/, and DIM-1/region/
    (the Nether). TU0 chunks already ARE Beta-McRegion structure, so block/entity/
    tile data is a direct copy; only the compression changes (LZX -> zlib).

    `owner` (profile XUID, hex or decimal) selects WHICH player becomes the Java
    player when the world holds many `players/<XUID>.dat`. It must come from the save
    itself -- the STFS/CON header profile id -- NOT the file path. Without it (a bare
    extracted savegame.dat carries no owner), the first player entry is used."""
    import shutil
    import time as _time
    w = lce.World.open(lce_save, owner=owner)
    os.makedirs(out_dir, exist_ok=True)
    overworld, nether = [], []
    for name in [n for n in w._filedata if n.endswith(".mcr")]:
        if name.startswith("DIM-1"):
            rx, rz = (int(p) for p in name[5:-4].split(".")[1:3]); nether.append((rx, rz))
        elif name.startswith("DIM1/"):
            continue                                     # the End doesn't exist in Beta 1.6.6
        else:
            rx, rz = (int(p) for p in name[:-4].split(".")[1:3]); overworld.append((rx, rz))
    n_ow = _to_java_dim(w, out_dir, overworld, False)
    n_ne = _to_java_dim(w, os.path.join(out_dir, "DIM-1"), nether, True) if nether else 0
    _write_java_level(w, out_dir, level_name)
    # extras that make it a real, loadable Beta world
    shutil.copyfile(os.path.join(out_dir, "level.dat"), os.path.join(out_dir, "level.dat_old"))
    with open(os.path.join(out_dir, "session.lock"), "wb") as f:
        f.write(struct.pack(">q", int(_time.time() * 1000)))
    base = lce_save if os.path.isdir(lce_save) else os.path.dirname(lce_save)
    thumb = os.path.join(base, "__thumbnail.png")
    if os.path.exists(thumb):
        shutil.copyfile(thumb, os.path.join(out_dir, "icon.png"))
    log("wrote Java world: overworld %d chunks, nether %d chunks -> %s" % (n_ow, n_ne, out_dir))
    return out_dir
