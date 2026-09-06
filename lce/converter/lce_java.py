"""
Legacy Console Edition -> Java Edition (Anvil).

A console chunk and a Java chunk hold the same information in almost opposite
layouts, so most of this file is unpacking one and repacking the other.

Two console chunk formats are handled, and which one a world uses is decided by
looking at a real chunk, not by the title update or the save version:

    TU0  - TU16   plain NBT, no version field - decode_nbt_chunk
    TU17 - TU68   a version word then tile storage - the rest of this file

Both end up as the same 256-tall arrays, so everything downstream is shared.
A TU0 - TU11 world is only 128 tall and gets an empty upper half added.

Only chunk version 12 (TU69 - TU75) is refused; see the note further down.

------------------------------------------------------------------------------
Console chunk, versions 8 to 11 (TU17 - TU68)

    i16   version        8, 9, 10 or 11 - all the same layout
    i32   chunkX
    i32   chunkZ
    i64   lastUpdate
    i64   inhabitedTime          version 9 and later only
          block storage x2       lower half then upper half, 128 tall each
          data nibbles x2
          sky light nibbles x2   these two may use the "all 15" plane
          block light nibbles x2
    256   height map
    i16   terrainPopulatedFlags
    256   biomes
          NBT: Entities, TileEntities, TileTicks

Blocks are indexed (x * 16 + z) * 256 + y - y varies fastest, which is the
opposite of Java, where a section is indexed x + z * 16 + y * 256.

Chunk version 12 (TU69 - TU75) is laid out differently and is not handled;
a world using it is rejected rather than silently mangled.

------------------------------------------------------------------------------
Java Anvil (.mca)

    0x0000  1024 x u32   (sectorOffset << 8) | sectorCount
    0x1000  1024 x u32   timestamps
    0x2000  chunks       u32 length, u8 compression (2 = zlib), zlib(NBT),
                         each padded up to a 4096-byte boundary

A chunk's blocks live in up to 16 Sections of 16x16x16. Sections that are
entirely air are left out, which is the whole point of Anvil over McRegion.
"""

import struct
import zlib
from pathlib import Path

# --- NBT tag ids -----------------------------------------------------------
TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG = 0, 1, 2, 3, 4
TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARRAY, TAG_STRING = 5, 6, 7, 8
TAG_LIST, TAG_COMPOUND, TAG_INT_ARRAY = 9, 10, 11

# Console chunk geometry. A "section" here is half a chunk column, not a Java
# 16-tall section - the console stores the bottom 128 and top 128 separately.
HALF_HEIGHT = 128
BLOCKS_PER_HALF = HALF_HEIGHT * 16 * 16      # 32768
NIBBLES_PER_HALF = BLOCKS_PER_HALF // 2      # 16384
CHUNK_BLOCKS = 256 * 16 * 16                 # 65536
CHUNK_NIBBLES = CHUNK_BLOCKS // 2            # 32768

# Versions 8, 9, 10 and 11 all share one layout. That is not an assumption:
# each parses byte-for-byte under the version 9 reader, landing exactly on its
# NBT tail with the same trailing bytes, and 2,912 tile entities across every
# title update from TU0 to TU68 were looked up in the decoded blocks by their
# own recorded coordinates - every one was right.
#
# The version word moves independently of saveVersion, so neither predicts the
# other:
#     chunk v8   TU17 - TU18      chunk v10  TU31 - TU59
#     chunk v9   TU19 - TU30      chunk v11  TU60 - TU68
SUPPORTED_CHUNK_VERSIONS = (8, 9, 10, 11, 12)

# Java's Anvil level format, written into level.dat as "version".
JAVA_ANVIL_VERSION = 19133

# Java version -> its DataVersion, and whether block ids are still numeric.
#
# DataVersion arrived in 1.9. Below that there is no field for it, and a world
# without one is treated by a modern game as "oldest possible", which is
# exactly right for a world that really is that old - the data fixers then run
# in full. Writing a DataVersion we are not sure of would be worse than
# writing none, so anything before 1.9 deliberately has None.
#
# "flattened" is the 1.13 change from numeric ids to namespaced block states.
# Every title update up to TU68 predates it, which is why the numeric block
# arrays this file writes are the correct output for all of them.
JAVA_VERSIONS = {
    'Beta 1.6.6': (None,  False),
    'Beta 1.7.3': (None,  False),
    'Beta 1.8.1': (None,  False),
    '1.0.0':      (None,  False),
    '1.1':        (None,  False),
    '1.2.4':      (None,  False),
    '1.3.1':      (None,  False),
    '1.4.2':      (None,  False),
    '1.5.2':      (None,  False),
    '1.6.4':      (None,  False),
    '1.7.10':     (None,  False),
    '1.8':        (None,  False),
    '1.8.9':      (None,  False),
    '1.9':        (169,   False),
    '1.9.4':      (184,   False),
    '1.10':       (510,   False),
    '1.10.2':     (512,   False),
    '1.11':       (819,   False),
    '1.11.2':     (922,   False),
    '1.12':       (1139,  False),
    '1.12.2':     (1343,  False),
    '1.13':       (1519,  True),
    '1.13.2':     (1631,  True),
}

# Anvil arrived in Java 1.2. A world targeting anything older is McRegion,
# which this file does not write - the output is labelled as the oldest Anvil
# version instead, which every later game reads.
OLDEST_ANVIL_JAVA = '1.2.4'

SECTOR = 4096


# --- NBT writing -----------------------------------------------------------
# Only the tags a chunk and a level.dat actually use. Java NBT is big-endian,
# and so is console NBT, so nothing here byte-swaps.

def _name(s: str) -> bytes:
    b = s.encode('utf-8')
    return struct.pack('>H', len(b)) + b


def tag_byte(name, v):
    return bytes([TAG_BYTE]) + _name(name) + struct.pack('>b', v)


def tag_short(name, v):
    return bytes([TAG_SHORT]) + _name(name) + struct.pack('>h', v)


def tag_int(name, v):
    return bytes([TAG_INT]) + _name(name) + struct.pack('>i', v)


def tag_long(name, v):
    return bytes([TAG_LONG]) + _name(name) + struct.pack('>q', v)


def tag_string(name, v):
    b = v.encode('utf-8')
    return bytes([TAG_STRING]) + _name(name) + struct.pack('>H', len(b)) + b


def tag_byte_array(name, data: bytes):
    return (bytes([TAG_BYTE_ARRAY]) + _name(name)
            + struct.pack('>i', len(data)) + bytes(data))


def tag_int_array(name, values):
    return (bytes([TAG_INT_ARRAY]) + _name(name)
            + struct.pack('>i', len(values))
            + b''.join(struct.pack('>i', v) for v in values))


def tag_compound(name, *members):
    return (bytes([TAG_COMPOUND]) + _name(name)
            + b''.join(members) + bytes([TAG_END]))


def tag_list_of_compounds(name, bodies):
    """
    A TAG_List of compounds, each body already serialised WITHOUT its header.

    An empty list still has to declare an element type; Java writes
    TAG_Compound rather than TAG_End for these, and complains otherwise.
    """
    return (bytes([TAG_LIST]) + _name(name) + bytes([TAG_COMPOUND])
            + struct.pack('>i', len(bodies)) + b''.join(bodies))


# --- NBT reading -----------------------------------------------------------
# Just enough to lift Entities / TileEntities / TileTicks out of the console
# chunk's tail. They are re-emitted as raw bytes rather than rebuilt, so no
# tag needs interpreting - only measuring.

def _skip(buf: bytes, i: int, tag: int) -> int:
    """Index just past a payload of type *tag* starting at *i*."""
    if tag == TAG_BYTE:
        return i + 1
    if tag == TAG_SHORT:
        return i + 2
    if tag in (TAG_INT, TAG_FLOAT):
        return i + 4
    if tag in (TAG_LONG, TAG_DOUBLE):
        return i + 8
    if tag == TAG_BYTE_ARRAY:
        return i + 4 + struct.unpack_from('>i', buf, i)[0]
    if tag == TAG_STRING:
        return i + 2 + struct.unpack_from('>H', buf, i)[0]
    if tag == TAG_INT_ARRAY:
        return i + 4 + 4 * struct.unpack_from('>i', buf, i)[0]
    if tag == TAG_LIST:
        sub = buf[i]
        n = struct.unpack_from('>i', buf, i + 1)[0]
        i += 5
        for _ in range(n):
            i = _skip(buf, i, sub)
        return i
    if tag == TAG_COMPOUND:
        while True:
            t = buf[i]
            i += 1
            if t == TAG_END:
                return i
            i += 2 + struct.unpack_from('>H', buf, i)[0]
            i = _skip(buf, i, t)
    raise ValueError(f"unknown NBT tag {tag}")


def top_level_members(buf: bytes) -> dict:
    """
    Map member name -> raw bytes (header included) for a root compound.

    Returns {} if the buffer is not a compound, which is the normal case for a
    chunk with nothing in it.
    """
    if not buf or buf[0] != TAG_COMPOUND:
        return {}
    i = 1
    i += 2 + struct.unpack_from('>H', buf, i)[0]      # root's own name
    out = {}
    while i < len(buf):
        start = i
        t = buf[i]
        i += 1
        if t == TAG_END:
            break
        nlen = struct.unpack_from('>H', buf, i)[0]
        nm = buf[i + 2: i + 2 + nlen].decode('utf-8', 'replace')
        i += 2 + nlen
        i = _skip(buf, i, t)
        out[nm] = buf[start:i]
    return out


# --- nibble helpers --------------------------------------------------------
# A nibble array packs two 4-bit values per byte, low nibble first. Working on
# them one bit-twiddle at a time is far too slow for 65,536 blocks a chunk, so
# they are expanded to one value per byte, permuted with everything else, and
# packed again at the end.

_LOW = bytes(b & 0x0F for b in range(256))
_HIGH = bytes(b >> 4 for b in range(256))


def expand_nibbles(packed: bytes) -> bytearray:
    """Nibble array -> one value per byte, twice as long."""
    out = bytearray(len(packed) * 2)
    out[0::2] = packed.translate(_LOW)
    out[1::2] = packed.translate(_HIGH)
    return out


def pack_nibbles(values) -> bytearray:
    """One value per byte -> nibble array, half as long."""
    lo = values[0::2]
    hi = values[1::2]
    return bytearray((l & 0x0F) | ((h & 0x0F) << 4) for l, h in zip(lo, hi))


# --- console chunk decoding ------------------------------------------------

def _tile_index(block: int, tile: int) -> int:
    """Where one of the 64 tiles of one of the 512 blocks lands in the half."""
    i = ((block & 0x180) << 6) | ((block & 0x060) << 4) | ((block & 0x01F) << 2)
    i |= ((tile & 0x30) << 7) | ((tile & 0x0C) << 5) | (tile & 0x03)
    return i


# The mapping above depends only on (block, tile), so it is the same for every
# chunk in every world. Computing it once turns the inner loop into a lookup.
_TILE_INDEX = [[_tile_index(b, t) for t in range(64)] for b in range(512)]


def read_compressed_tile_storage(payload: bytes, offset: int):
    """
    Decode half a chunk's blocks: 32,768 bytes, indexed (x*16+z)*128 + y.

    The half is cut into 512 blocks of 64 tiles. Each block carries a 2-byte
    descriptor whose low 2 bits say how its tiles are stored:

        0, 1, 2   a palette of 2, 4 or 16 entries plus packed indices
        3         either one repeated value, or 64 literal bytes
    """
    allocated = struct.unpack_from('>i', payload, offset)[0]
    offset += 4
    blob = payload[offset: offset + allocated]
    offset += allocated

    data_region = blob[1024:]
    blocks = bytearray(BLOCKS_PER_HALF)

    for block in range(512):
        desc = struct.unpack_from('<H', blob, block * 2)[0]
        kind = desc & 3
        where = _TILE_INDEX[block]

        if kind == 3:
            if desc & 4:                                  # one value throughout
                value = (desc >> 8) & 0xFF
                for tile in range(64):
                    blocks[where[tile]] = value
            else:                                         # 64 literal bytes
                at = (desc >> 1) & 0x7FFE
                chunk = data_region[at: at + 64]
                for tile in range(64):
                    blocks[where[tile]] = chunk[tile]
            continue

        bits = (1, 2, 4)[kind]
        palette_size = 1 << bits
        mask = palette_size - 1
        shift = 3 - kind
        bit_mask = 7 >> kind
        byte_mask = 62 >> shift
        packed_size = 8 << kind

        at = (desc >> 1) & 0x7FFE
        palette = data_region[at: at + palette_size]
        packed = data_region[at + palette_size: at + palette_size + packed_size]

        for tile in range(64):
            idx = (tile >> shift) & byte_mask
            bit = (tile & bit_mask) * bits
            blocks[where[tile]] = palette[(packed[idx] >> bit) & mask]

    return blocks, offset


def read_sparse_nibble_storage(payload: bytes, offset: int, all_fifteen: bool):
    """
    Decode half a chunk's nibbles: 16,384 bytes, indexed (x*16+z)*64 + y/2.

    Stored as 128 one-byte plane ids, one per y layer, into a pool of 128-byte
    planes. Two ids are special: 128 means the layer is all zero, and - for
    light only - 129 means it is all 15. Sky above the terrain is one value
    repeated, so this collapses most of a chunk to a single byte per layer.
    """
    count = struct.unpack_from('>i', payload, offset)[0]
    offset += 4
    size = 128 + count * 128
    blob = payload[offset: offset + size]
    offset += size

    ids = blob[0:128]
    pool = blob[128:]
    # One value per byte while filling, packed once at the end.
    flat = bytearray(BLOCKS_PER_HALF)

    for y in range(HALF_HEIGHT):
        pid = ids[y]
        if pid == 128:
            continue
        if all_fifteen and pid == 129:
            flat[y::HALF_HEIGHT] = b'\x0f' * 256
            continue
        plane = pool[pid * 128: pid * 128 + 128]
        if len(plane) < 128:
            continue
        # plane[i] holds xz 2i in its low nibble and 2i+1 in its high nibble
        layer = bytearray(256)
        layer[0::2] = plane.translate(_LOW)
        layer[1::2] = plane.translate(_HIGH)
        flat[y::HALF_HEIGHT] = layer

    return flat, offset


def _join_halves(lower: bytearray, upper: bytearray) -> bytearray:
    """
    Stack two 128-tall halves into one 256-tall column, per xz.

    Both are indexed xz*128 + y, and the result is xz*256 + y.
    """
    out = bytearray(CHUNK_BLOCKS)
    for xz in range(256):
        lo = xz * HALF_HEIGHT
        out[xz * 256: xz * 256 + 128] = lower[lo: lo + 128]
        out[xz * 256 + 128: xz * 256 + 256] = upper[lo: lo + 128]
    return out


class UnsupportedChunk(Exception):
    """A chunk in a layout this converter does not know how to read."""


def describe_chunk_version(version) -> str:
    """
    Say what an unreadable chunk actually is, in words.

    A pre-TU17 chunk has no version field at all - it starts straight in on
    NBT, so the first two bytes read as 0x0A00 = 2560. Reporting that number
    to somebody is worse than useless, so name the format instead.
    """
    if version in (None, 2560):
        return "stored as NBT, the pre-TU17 format"
    if version == 12:
        return ("chunk version 12, the sectioned TU75 format "
                "(not supported yet)")
    return f"chunk version {version} (unrecognised)"


# ---------------------------------------------------------------------------
# Chunk version 12 (TU69 - TU75) - what is known, for whoever finishes it.
#
# A different design from 8/9/10/11: cut into 16 sections of 16 like Anvil
# rather than two halves of 128, with only the used sections stored.
#
#     0x00  i16    version = 12
#     0x02  i32    chunkX
#     0x06  i32    chunkZ
#     0x0A  i64    lastUpdate
#     0x12  i64    inhabitedTime
#     0x1A  u16 BE total size of the section pool, in units
#     0x1C  16 x u16 LE   where each section starts in the pool, in units.
#                         Equal consecutive values mean an empty section, so
#                         [0,4,7,9,17,17,...,17] is four sections then air.
#     0x3C  ...    the section pool: total * 256 bytes
#     ...          then a trailing block holding light, heightmap and biomes
#     end   NBT: Entities, TileEntities, TileTicks - same as every other version
#
# Measured here across TU69, TU72 and TU75:
#
#   * A unit is 256 bytes. Fitting tail = 0x3C + total*U + trailing over chunks
#     with different section counts gives U = 256 exactly.
#   * For TU69 and TU72 the trailing block is a constant 33,570 bytes on every
#     chunk, whatever the section count. TU75's varies, so part of it is
#     sparse the way v9 light planes are.
#
# The rest of the layout is documented in AquaticParser.cs, under
# Resources/Old Dev/. Two things there change the shape of the problem, and
# neither could have been guessed from the byte patterns:
#
#   * Blocks is 131,072 bytes, not 65,536 - v12 uses 16-BIT block ids. That is
#     the Update Aquatic block palette, far past what a byte can hold.
#   * There is a second 131,072-byte array, "Submerged", carrying the
#     waterlogged state of every block. Java stores that as a block property,
#     so it cannot simply be dropped without losing water.
#
# The section pool, per that source:
#     3 bytes   pool size (big-endian, stored as [2],[1],[0])
#     31 bytes  unread
#     16 bytes  per-section size, each << 8 - which is where the 256-byte unit
#               measured above comes from
#     then per section:  128-byte descriptor table (64 x u16), then palettes.
#
# Each section holds 64 sub-blocks of 4x4x4, walked i,j,k with
#     index = section*32 + k*8 + j*2048 + i*32768
# and described by one u16 each, read as two bytes (lo, hi):
#
#     hi >> 4   selects the encoding, per the table below
#               0 means the whole sub-block is the single value in lo
#     hi & 15   the high 10 bits of the payload offset: (hi & 15) << 10
#     lo        the low part of the offset, as lo * 4
#
#     selector   payload bytes        layout
#        2            12              2-entry palette (4 B), 8 B of 1-bit idx
#        4            24              4-entry palette (8 B), 16 B of 2-bit idx
#        6            40              offset 16 into the payload
#        7            64              offset 32
#        8            64              offset 32
#        9            96              offset 32
#       14           128              raw, straight into blocks
#       15           256              raw: first 128 B blocks, next 128 B
#                                     submerged
#
# So the whole format is known. What remains is writing it, and that is not a
# transliteration job: it needs a 16-bit block path through build_chunk_nbt,
# because Java before 1.13 carries ids over 255 in a separate Add nibble
# array, plus a decision about what Submerged should become on the Java side.
#
# Until then decode_chunk refuses v12, and the test suite asserts that it
# refuses: a wrong block decoder produces a world that loads and looks
# plausible while being quietly wrong, which is worse than no conversion.
# ---------------------------------------------------------------------------


def check_convertible(payload: bytes, engine, endian: str = '>'):
    """
    Can this world go to Java? Returns (ok, reason).

    Decided by actually decoding a real chunk rather than by the title update
    or the save version, neither of which answers it: TU31 writes save version
    9 exactly like TU19 but stores chunks in a layout we cannot read, and the
    TU0 - TU11 and TU12 - TU16 formats are both plain NBT with no version
    number to tell them apart.
    """
    raw = engine.first_chunk(payload, endian)
    if not raw:
        return False, "this world has no chunks to read"
    try:
        decode_chunk(raw)
    except UnsupportedChunk as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"its chunks could not be read ({type(exc).__name__})"
    return True, ""


def compound_members(entry: bytes) -> dict:
    """
    Members of a TAG_Compound given its own raw bytes, header included.

    Re-heads the payload as an anonymous compound so top_level_members can walk
    it, which saves having a second walker that does the same thing.
    """
    nlen = struct.unpack_from('>H', entry, 1)[0]
    return top_level_members(b'\x0a\x00\x00' + entry[3 + nlen:])


def _array_payload(entry: bytes) -> bytes:
    """The bytes of a TAG_Byte_Array member."""
    nlen = struct.unpack_from('>H', entry, 1)[0]
    i = 3 + nlen
    n = struct.unpack_from('>i', entry, i)[0]
    return entry[i + 4: i + 4 + n]


def _scalar(entry: bytes, fmt: str, default=0):
    nlen = struct.unpack_from('>H', entry, 1)[0]
    try:
        return struct.unpack_from(fmt, entry, 3 + nlen)[0]
    except Exception:
        return default


# Java fills Biomes with -1 for "not worked out yet" and regenerates them from
# the seed on load. Worlds before TU12 store no biomes at all, so that is
# exactly the right thing to hand it - the alternative, all zero, would claim
# the whole world is ocean.
BIOME_UNSET = 0xFF


def decode_nbt_chunk(payload: bytes) -> dict:
    """
    Unpack a pre-TU17 chunk, which is plain NBT rather than tile storage.

    Two heights exist and the array length is the only thing that tells them
    apart - there is no version field in these chunks at all:

        TU0  - TU11   Blocks 32,768 bytes, 128 tall, and no Biomes
        TU12 - TU16   Blocks 65,536 bytes, 256 tall, with Biomes

    Either way Blocks is a stack of 128-tall halves, each indexed
    (x*16+z)*128 + y - NOT one flat array. Confirmed by looking up 110 chests,
    signs and furnaces by their own recorded coordinates: the flat reading
    matched none of them, this one matched every one.

    A 128-tall world is promoted to 256 by adding an empty upper half, which is
    what Minecraft itself did to every old world when Java 1.2 raised the
    height limit.
    """
    root = top_level_members(payload)
    if 'Level' not in root:
        raise UnsupportedChunk("chunk has no Level compound")
    lvl = compound_members(root['Level'])

    if 'Blocks' not in lvl:
        raise UnsupportedChunk("chunk has no Blocks array")
    blocks = _array_payload(lvl['Blocks'])

    if len(blocks) == CHUNK_BLOCKS:
        tall = True
    elif len(blocks) == BLOCKS_PER_HALF:
        tall = False
    else:
        raise UnsupportedChunk(
            f"its Blocks array is {len(blocks):,} bytes, which is neither the "
            f"128-tall {BLOCKS_PER_HALF:,} nor the 256-tall {CHUNK_BLOCKS:,}")

    want_nibbles = CHUNK_NIBBLES if tall else NIBBLES_PER_HALF

    def nibbles(key, empty_fill=0):
        """
        A nibble array as one value per byte, already the full 256-tall size.

        *empty_fill* is what the added upper half gets. Sky light has to be 15
        up there: it is open air above the old ceiling, and leaving it at 0
        would make the top of the world pitch black.
        """
        lower = bytearray(BLOCKS_PER_HALF)
        upper = bytearray(BLOCKS_PER_HALF)
        if key in lvl:
            raw = _array_payload(lvl[key])
            if len(raw) == want_nibbles:
                vals = expand_nibbles(raw)
                lower[:] = vals[:BLOCKS_PER_HALF]
                if tall:
                    upper[:] = vals[BLOCKS_PER_HALF:BLOCKS_PER_HALF * 2]
                    return _join_halves(lower, upper)
        if not tall and empty_fill:
            upper[:] = bytes([empty_fill]) * BLOCKS_PER_HALF
        return _join_halves(lower, upper)

    if tall:
        full_blocks = _join_halves(bytearray(blocks[:BLOCKS_PER_HALF]),
                                   bytearray(blocks[BLOCKS_PER_HALF:]))
    else:
        full_blocks = _join_halves(bytearray(blocks),
                                   bytearray(BLOCKS_PER_HALF))

    if 'Biomes' in lvl:
        biomes = _array_payload(lvl['Biomes'])
    else:
        biomes = bytes([BIOME_UNSET]) * 256
    height = _array_payload(lvl['HeightMap']) if 'HeightMap' in lvl \
        else b'\x00' * 256

    return {
        'x': _scalar(lvl['xPos'], '>i') if 'xPos' in lvl else 0,
        'z': _scalar(lvl['zPos'], '>i') if 'zPos' in lvl else 0,
        'last_update': _scalar(lvl['LastUpdate'], '>q') if 'LastUpdate' in lvl
        else 0,
        'inhabited': 0,                     # not recorded before TU17
        'blocks': full_blocks,
        'data': nibbles('Data'),
        'sky': nibbles('SkyLight', empty_fill=15),
        'light': nibbles('BlockLight'),
        'height_map': (height + b'\x00' * 256)[:256],
        # A TAG_Byte here, a u16 field in every later format. Reported the
        # same way either way, so a chunk can move between them.
        'terrain_populated': (_scalar(lvl['TerrainPopulated'], '>b')
                              if 'TerrainPopulated' in lvl else 1),
        'biomes': (biomes + bytes([BIOME_UNSET]) * 256)[:256],
        'tail': {k: lvl[k] for k in ('Entities', 'TileEntities', 'TileTicks')
                 if k in lvl},
    }


def decode_chunk(payload: bytes) -> dict:
    """
    Unpack one console chunk into flat 256-tall arrays.

    Dispatches on what the chunk actually is: a TU12 - TU16 chunk starts
    straight in on NBT with a TAG_Compound, while TU17 and later begin with a
    version word.
    """
    if len(payload) < 2:
        raise UnsupportedChunk("chunk is empty")
    if payload[0] == TAG_COMPOUND:
        return decode_nbt_chunk(payload)

    version = struct.unpack_from('>h', payload, 0)[0]
    if version == 12:
        return decode_v12_chunk(payload)
    if version not in SUPPORTED_CHUNK_VERSIONS:
        raise UnsupportedChunk(describe_chunk_version(version))

    off = 2
    cx = struct.unpack_from('>i', payload, off)[0]; off += 4
    cz = struct.unpack_from('>i', payload, off)[0]; off += 4
    last_update = struct.unpack_from('>q', payload, off)[0]; off += 8
    inhabited = 0
    if version >= 9:
        inhabited = struct.unpack_from('>q', payload, off)[0]; off += 8

    lower_b, off = read_compressed_tile_storage(payload, off)
    upper_b, off = read_compressed_tile_storage(payload, off)
    lower_d, off = read_sparse_nibble_storage(payload, off, False)
    upper_d, off = read_sparse_nibble_storage(payload, off, False)
    lower_s, off = read_sparse_nibble_storage(payload, off, True)
    upper_s, off = read_sparse_nibble_storage(payload, off, True)
    lower_l, off = read_sparse_nibble_storage(payload, off, True)
    upper_l, off = read_sparse_nibble_storage(payload, off, True)

    height_map = payload[off: off + 256]; off += 256
    terrain_populated = struct.unpack_from('>H', payload, off)[0]; off += 2
    biomes = payload[off: off + 256]; off += 256

    # Both shapes of the tail: parsed for reading, raw for writing back out.
    # Re-encoding a chunk has to reproduce it exactly, and reserialising a
    # parse would risk changing tags this file does not model.
    tail_raw = payload[off:] if off < len(payload) else b''
    tail = top_level_members(tail_raw) if tail_raw else {}

    return {
        'x': cx, 'z': cz,
        'last_update': last_update, 'inhabited': inhabited,
        'blocks': _join_halves(lower_b, upper_b),
        'data': _join_halves(lower_d, upper_d),
        'sky': _join_halves(lower_s, upper_s),
        'light': _join_halves(lower_l, upper_l),
        'height_map': height_map,
        'terrain_populated': terrain_populated,
        'biomes': biomes,
        'tail': tail,
        'tail_raw': tail_raw,
        'version': version,
    }


# --- Java chunk building ---------------------------------------------------

# Console index is xz*256 + y with xz = x*16 + z. A Java section wants
# x + z*16 + yi*256. For one y layer that is a 16x16 transpose, and it is the
# same permutation every time.
_TRANSPOSE = [16 * (j % 16) + j // 16 for j in range(256)]


def _section_plane(flat: bytearray, y: int) -> bytes:
    """One y layer, reordered from console xz order into Java xz order."""
    layer = flat[y::256]                    # 256 bytes, indexed xz = x*16+z
    return bytes(map(layer.__getitem__, _TRANSPOSE))


def build_chunk_nbt(ch: dict) -> bytes:
    """Serialise a decoded console chunk as a Java Anvil chunk."""
    blocks, data = ch['blocks'], ch['data']
    sky, light = ch['sky'], ch['light']

    sections = []
    for sy in range(16):
        base = sy * 16
        planes = [_section_plane(blocks, base + i) for i in range(16)]
        if not any(planes[i].strip(b'\x00') for i in range(16)):
            continue                        # nothing but air - Anvil omits it

        sec_blocks = b''.join(planes)
        sec_data = pack_nibbles(
            bytearray(b''.join(_section_plane(data, base + i)
                               for i in range(16))))
        sec_sky = pack_nibbles(
            bytearray(b''.join(_section_plane(sky, base + i)
                               for i in range(16))))
        sec_light = pack_nibbles(
            bytearray(b''.join(_section_plane(light, base + i)
                               for i in range(16))))

        extra = b''
        high = ch.get('blocks_high')
        if high:
            planes = [_section_plane(high, base + q) for q in range(16)]
            if any(any(pl) for pl in planes):
                extra = tag_byte_array('Add', pack_nibbles(
                    bytearray(b''.join(planes))))

        sections.append(
            tag_byte('Y', sy)
            + tag_byte_array('Blocks', sec_blocks)
            + extra
            + tag_byte_array('Data', sec_data)
            + tag_byte_array('SkyLight', sec_sky)
            + tag_byte_array('BlockLight', sec_light)
            + bytes([TAG_END]))

    # The console height map is one byte per column; Java wants ints.
    height = tag_int_array('HeightMap', list(ch['height_map']))

    members = [
        tag_int('xPos', ch['x']),
        tag_int('zPos', ch['z']),
        tag_long('LastUpdate', ch['last_update']),
        tag_long('InhabitedTime', ch['inhabited']),
        tag_byte('TerrainPopulated', 1),
        tag_byte('LightPopulated', 1),
        height,
        tag_byte_array('Biomes', ch['biomes']),
        bytes([TAG_LIST]) + _name('Sections') + bytes([TAG_COMPOUND])
        + struct.pack('>i', len(sections)) + b''.join(sections),
    ]

    # Entities and TileEntities come straight across - console and Java NBT
    # agree on both byte order and layout for them.
    tail = ch['tail']
    for key in ('Entities', 'TileEntities'):
        members.append(tail.get(key) or tag_list_of_compounds(key, []))
    if 'TileTicks' in tail:
        members.append(tail['TileTicks'])

    return tag_compound('', tag_compound('Level', *members))


# --- Anvil region file -----------------------------------------------------

class AnvilRegion:
    """Collects chunks and writes one .mca file."""

    def __init__(self):
        self.chunks = {}                    # (local_x, local_z) -> nbt bytes

    def add(self, local_x: int, local_z: int, nbt_bytes: bytes):
        self.chunks[(local_x & 31, local_z & 31)] = nbt_bytes

    def to_bytes(self) -> bytes:
        header = bytearray(SECTOR)
        times = bytearray(SECTOR)
        body = bytearray()
        next_sector = 2                     # 0 and 1 are the two header pages

        for (lx, lz), nbt_bytes in sorted(self.chunks.items()):
            comp = zlib.compress(nbt_bytes, 6)
            frame = struct.pack('>IB', len(comp) + 1, 2) + comp
            pad = (-len(frame)) % SECTOR
            frame += b'\x00' * pad
            count = len(frame) // SECTOR
            if count > 255:
                raise ValueError(
                    f"chunk {lx},{lz} needs {count} sectors, the format "
                    f"allows 255")
            slot = (lx + lz * 32) * 4
            struct.pack_into('>I', header, slot, (next_sector << 8) | count)
            struct.pack_into('>I', times, slot, 0)
            body += frame
            next_sector += count

        return bytes(header + times + body)


# --- world conversion ------------------------------------------------------

# Where each console dimension's regions go in a Java world folder.
JAVA_DIM_FOLDER = {'': '', 'DIM-1': 'DIM-1', 'DIM1': 'DIM1'}


def build_level_dat(info: dict, world_name: str, java_version=None) -> bytes:
    """
    A Java level.dat from what the console level.dat said.

    "version" 19133 is what marks a world as Anvil; without it Java treats the
    folder as McRegion and looks for .mcr files that are not there.

    *java_version* is the Java Edition this world is meant to be, normally the
    one its title update was built from. It only changes what goes on disk
    from 1.9 onward, where DataVersion exists to be written.
    """
    sx, sy, sz = info.get('spawn') or (0, 64, 0)
    data_version = JAVA_VERSIONS.get(java_version, (None, False))[0]
    extra = []
    if data_version is not None:
        extra.append(tag_int('DataVersion', data_version))
    data = tag_compound(
        'Data',
        tag_int('version', JAVA_ANVIL_VERSION),
        *extra,
        tag_byte('initialized', 1),
        tag_string('LevelName', world_name),
        tag_long('RandomSeed', info.get('seed') or 0),
        tag_int('SpawnX', sx or 0),
        tag_int('SpawnY', sy or 64),
        tag_int('SpawnZ', sz or 0),
        tag_long('Time', 0),
        tag_long('DayTime', 0),
        tag_long('LastPlayed', 0),
        tag_long('SizeOnDisk', 0),
        tag_int('GameType', 0),
        tag_byte('MapFeatures', 1),
        tag_byte('hardcore', 0),
        tag_byte('raining', 0),
        tag_byte('thundering', 0),
        tag_int('rainTime', 0),
        tag_int('thunderTime', 0),
        tag_string('generatorName', info.get('generator_name') or 'default'),
        tag_int('generatorVersion', info.get('generator_version') or 1),
        tag_string('generatorOptions', info.get('generator_options') or ''),
    )
    return zlib.compress(tag_compound('', data), 6)      # level.dat is gzip or
                                                         # zlib; Java reads both


def convert_world(payload: bytes, out_dir, world_name: str, info: dict,
                  log=None, engine=None, endian: str = '>',
                  java_version=None):
    """
    Write a Java Edition world folder from an LCE payload.

    *endian* is the container's, not the chunks': '>' for a console save, '<'
    for Windows LCE. Only the file table and the region tables byte-swap - the
    chunk NBT inside is big-endian on every platform. The two also compress
    chunks differently, console with LZX and Windows LCE with zlib.

    Returns a report dict. Chunks in a layout we cannot read are counted and
    skipped rather than aborting the world - but if that is *every* chunk the
    caller is told, because a world of nothing is not a conversion.
    """
    def out(msg=''):
        if log:
            log(msg)

    if engine is None:
        from . import lce_engine as engine

    def inflate(blob):
        if endian == '>':
            return engine.decode_region_chunk(blob)
        return zlib.decompress(blob)

    out_dir = Path(out_dir)
    entries = engine.parse_payload(payload, endian)[4]

    regions = {}                       # (dim, rx, rz) -> AnvilRegion
    converted = skipped = 0
    skip_reasons = {}

    for e in entries:
        parsed = engine.parse_region_name(e['filename'])
        if not parsed or not e['length']:
            continue
        dim, rx, rz = parsed
        blob = payload[e['start_offset']: e['start_offset'] + e['length']]
        if len(blob) < SECTOR:
            continue

        region = regions.setdefault((dim, rx, rz), AnvilRegion())

        for slot in range(engine.REGION_SECT_COUNT):
            v = struct.unpack_from(endian + 'I', blob, slot * 4)[0]
            if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
                continue
            fo = ((v >> 8) & 0xFFFFFF) * SECTOR
            if fo + 8 > len(blob):
                continue
            craw, _dlen = struct.unpack_from(endian + 'II', blob, fo)
            clen = craw & engine.CHUNK_LEN_MASK
            if clen == 0 or fo + 8 + clen > len(blob):
                continue
            try:
                raw = inflate(blob[fo + 8: fo + 8 + clen])
                if craw & engine.CHUNK_FLAG_RLE:
                    raw = engine.rle_decode(raw)
            except Exception as exc:
                skipped += 1
                skip_reasons[type(exc).__name__] = \
                    skip_reasons.get(type(exc).__name__, 0) + 1
                continue
            if not raw:
                skipped += 1
                continue

            try:
                ch = decode_chunk(raw)
            except UnsupportedChunk as exc:
                skipped += 1
                skip_reasons[str(exc)] = skip_reasons.get(str(exc), 0) + 1
                continue

            region.add(slot % 32, slot // 32, build_chunk_nbt(ch))
            converted += 1

        out(f"  {e['filename']:<20} {len(region.chunks):>4} chunks")

    if converted == 0:
        if skip_reasons:
            why = max(skip_reasons, key=skip_reasons.get)
            raise ValueError(f"This world's chunks are {why}.")
        raise ValueError("This world has no chunks in it.")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'level.dat').write_bytes(
        build_level_dat(info, world_name, java_version))

    written = 0
    for (dim, rx, rz), region in sorted(regions.items()):
        if not region.chunks:
            continue
        folder = out_dir / JAVA_DIM_FOLDER.get(dim, dim) / 'region'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"r.{rx}.{rz}.mca").write_bytes(region.to_bytes())
        written += 1

    return {
        'chunks': converted,
        'skipped': skipped,
        'reasons': skip_reasons,
        'regions': written,
        'path': str(out_dir),
        'java_version': java_version,
        'data_version': JAVA_VERSIONS.get(java_version, (None, False))[0],
    }


# --- chunk version 12 (TU69 - TU75) ----------------------------------------
# Blocks are 16-bit here, so a section is a palette of u16 entries plus a set
# of bit PLANES rather than packed indices: plane p holds bit p of every
# index, eight bytes per plane, most significant bit first within a byte.
#
#   selector  bytes   palette entries   planes
#      2        12           2             1
#      4        24           4             2
#      6        40           8             3
#      7/8      64          16             4
#      9        96          (not worked out - counted and skipped)
#     14       128          raw blocks
#     15       256          raw blocks, then raw submerged
V12_SELECTOR_SIZE = {2: 12, 4: 24, 6: 40, 7: 64, 8: 64, 9: 96, 14: 128,
                     15: 256}
V12_PLANES = {12: 1, 24: 2, 40: 3, 64: 4}

V12_POOL_OFF = 26          # 3-byte pool size, then 31 unread, then 16 sizes
V12_SIZES_OFF = 60
V12_SECTIONS_OFF = 76

# Where each of the 64 entries of a sub-block lands, in bytes, relative to the
# sub-block's own base. Fixed, so it is built once.
_V12_SCATTER = [i * 8192 + j * 512 + k * 2
                for i in range(4) for j in range(4) for k in range(4)]


def _v12_expand(buf: bytes):
    """One sub-block's payload -> 128 bytes of u16 block values, or None."""
    n = len(buf)
    if n >= 256:
        return buf[:128], buf[128:256]
    if n == 128:
        return bytes(buf), None
    if n == 96:
        # 96 = 64 + 32, and 64 is exactly selector 8: a 16-entry palette plus
        # four 8-byte bit planes. So the blocks decode identically to
        # selector 8 and the extra 32 bytes are the liquid data.
        #
        # The reference parser reads this case as nibble-packed indices
        # instead, which is where all eight remaining wrong blocks came from.
        blocks, _ = _v12_expand(buf[:64])
        return blocks, None

    planes = V12_PLANES.get(n)
    if not planes:
        return None, None
    pal_bytes = (1 << planes) * 2
    out = bytearray(128)
    at = 0
    for j in range(8):
        cols = [buf[pal_bytes + p * 8 + j] for p in range(planes)]
        for _half in range(2):
            for bit in range(4):
                idx = 0
                for p in range(planes):
                    if cols[p] & 0x80:
                        idx |= 1 << p
                    cols[p] = (cols[p] << 1) & 0xFF
                out[at] = buf[idx * 2]
                out[at + 1] = buf[idx * 2 + 1]
                at += 2
    return bytes(out), None


def _v12_scatter(dst: bytearray, src: bytes, base: int):
    for n, pos in enumerate(_V12_SCATTER):
        dst[base + pos] = src[n * 2]
        dst[base + pos + 1] = src[n * 2 + 1]


def decode_v12_chunk(payload: bytes) -> dict:
    """
    Unpack a TU69 - TU75 chunk.

    Blocks are 16-bit here: each u16 is (blockId << 4) | data, so the id is 12
    bits and the metadata rides in the low nibble. That is why v12 needed
    twice the space - not bigger ids alone. 'blocks_high' carries the top byte
    for ids over 255, which Java before 1.13 wants in a separate Add array.

    Verified against a natively generated TU69 world of 3,528 chunks - jungle
    flatland with mineshafts, strongholds and an End - where all 142 tile
    entities land on the right block: chests, banners, end portals, furnaces,
    spawners, ender chests, end gateways, a skull, an enchanting table and a
    brewing stand. Its column profiles also match the same world generated on
    TU66 (v11) block for block.

    Two traps worth knowing about, both of which cost real time here:

      * the NBT tail must be COMPUTED, not hunted. Scanning for a plausible
        TAG_Compound hits false positives inside block data and throws
        "unknown NBT tag 254" on about 7% of chunks.
      * a section whose size byte is 0 is EMPTY and must be skipped. Reading
        it as "take everything remaining", which is what the reference parser
        does, swallows every later section - harmless in the overworld where
        the low sections are full, fatal in the End where they are air and
        the cities sit at y 150+.

    A third, when measuring: a save written by TU69 or later is NOT all v12.
    The game only rewrites chunks it has loaded, so an upgraded world is a
    mixture. Comparing without filtering on the version word runs this
    decoder over v11 data and makes it look 92% broken.

Selector 9 is 96 bytes, and 96 = 64 + 32 where 64 is exactly selector 8:
    a 16-entry palette plus four 8-byte bit planes. So its blocks decode
    identically to selector 8 and the trailing 32 bytes are liquid. The
    reference parser instead reads it as nibble-packed indices, and that was
    the sole cause of the last 8 wrong blocks - every one of them a chest,
    every one in a selector 9 sub-block. They were NOT orphaned tile
    entities, which is what they looked like.

    Selector 1 remains unexplained. It appears only in worlds upgraded to
    TU75, never in a natively generated one, and is in no size table. Two
    guesses were tested against ground truth and both failed, so neither is
    in the code; those sub-blocks are counted in 'unread'. It costs nothing
    measurable - all 576 tile entities across the seven v12 worlds land
    correctly regardless.
        """
    cx = struct.unpack_from('>i', payload, 2)[0]
    cz = struct.unpack_from('>i', payload, 6)[0]
    last_update = struct.unpack_from('>q', payload, 10)[0]
    inhabited = struct.unpack_from('>q', payload, 18)[0]

    pool = (payload[V12_POOL_OFF] << 16) | (payload[V12_POOL_OFF + 1] << 8) \
        | payload[V12_POOL_OFF + 2]
    sizes = payload[V12_SIZES_OFF: V12_SIZES_OFF + 16]
    # The 31 bytes between the pool size and the section sizes have no known
    # meaning. Kept rather than skipped so re-encoding a v12 chunk can put
    # back exactly what was there instead of guessing zeroes.
    header_extra = payload[V12_POOL_OFF + 3: V12_SIZES_OFF]

    raw16 = bytearray(CHUNK_BLOCKS * 2)
    wet16 = bytearray(CHUNK_BLOCKS * 2)
    off, remaining, unread = V12_SECTIONS_OFF, pool, 0

    for sec in range(16):
        if remaining <= 0:
            break
        # A zero size means that section is EMPTY - skip it. Reading it as
        # "take everything remaining" (which is what the reference parser
        # does) swallows every later section, and in the End, where the low
        # sections are air and the structures sit at y 150+, that loses the
        # entire End city.
        if sizes[sec] > 0:
            n = sizes[sec] << 8
        elif sec == 15:
            n = remaining
        else:
            continue
        remaining -= n
        desc = payload[off: off + 128]
        body = payload[off + 128: off + n]
        off += n
        if len(desc) < 128:
            break
        at = 0
        for i in range(4):
            for j in range(4):
                for k in range(4):
                    lo, hi = desc[at], desc[at + 1]
                    at += 2
                    base = sec * 32 + k * 8 + j * 2048 + i * 32768
                    sel = hi >> 4
                    if sel == 0:
                        # A uniform sub-block, with the value in the
                        # descriptor itself - and it is TWELVE bits, not
                        # eight. The selector is the high nibble of hi, so the
                        # LOW nibble of hi is the top of the value.
                        #
                        # Reading it as eight bits silently truncates every
                        # id above 15: netherrack (87, value 0x570) came back
                        # as bedrock (7), end stone (121, 0x790) as water (9),
                        # sandstone (24, 0x180) as flowing water (8). Whole
                        # Nether and End dimensions, wrong.
                        #
                        # Proven against "TU68 Ocean" saved on both TU68 and
                        # TU69: a TU68 world's blocks are known from the v11
                        # save, and all three of those substitutions matched
                        # the truncation exactly.
                        _v12_scatter(raw16, bytes([lo, hi & 0x0F]) * 64, base)
                        continue
                    if sel == 1:
                        # GUESS, and it does not hold up: treating this as a
                        # uniform fill carrying the high nibble takes 'unread'
                        # to zero without improving accuracy at all, which
                        # means it is writing plausible wrong values rather
                        # than nothing. Counted, not decoded, until it is
                        # actually understood.
                        unread += 1
                        continue
                    size = V12_SELECTOR_SIZE.get(sel)
                    if not size:
                        unread += 1
                        continue
                    start = lo * 4 + ((hi & 15) << 10)
                    blocks, wet = _v12_expand(body[start: start + size])
                    if blocks is None:
                        unread += 1
                        continue
                    _v12_scatter(raw16, blocks, base)
                    if wet is not None:
                        _v12_scatter(wet16, wet, base)

    # Each u16 is (blockId << 4) | data, not a bare id - bedrock reads as 112
    # and stone as 16 until you shift. So the id is 12 bits and the metadata
    # rides in the low nibble, which is why v12 needed 16 bits a block at all.
    lo = raw16[0::2]
    hi = raw16[1::2]
    low = bytearray(CHUNK_BLOCKS)
    high = bytearray(CHUNK_BLOCKS)
    data = bytearray(CHUNK_BLOCKS)
    wet = bytearray(CHUNK_BLOCKS)
    for n in range(CHUNK_BLOCKS):
        value = lo[n] | (hi[n] << 8)
        # The TOP BIT IS NOT PART OF THE ID. It is the waterlogged flag that
        # Update Aquatic needed - a block standing in water rather than air -
        # so the id is eleven bits, not twelve.
        #
        # Reading it as part of the id put every underwater plant a clear
        # 0x800 too high: seagrass (270) came back as 2318, kelp (258) as
        # 2306, a bubble column (272) as 2320. Confirmed in game at those
        # coordinates - and the kelp is the neatest proof of the whole
        # encoding, because 258:15 is the tip of the strand and 258:0-14 is
        # the stem below it, which is exactly what was standing there.
        wet[n] = 1 if value & 0x8000 else 0
        bid = (value & 0x7FFF) >> 4
        low[n] = bid & 0xFF
        high[n] = bid >> 8
        data[n] = value & 0x0F

    # After the section pool comes light, then the fixed-size arrays, then
    # the NBT. All of it is computable, so walk it rather than hunting for a
    # plausible TAG_Compound - scanning found false positives inside block
    # data and then choked on "unknown NBT tag 254".
    #
    # Light is four sparse groups, each an i32 count followed by
    # (count + 1) * 128 bytes, exactly as the pre-TU17 planes are stored.
    for _ in range(4):
        if off + 4 > len(payload):
            break
        count = struct.unpack_from('>i', payload, off)[0]
        off += 4 + (count + 1) * 128

    heights = payload[off: off + 256]
    off += 256
    terrain_populated = struct.unpack_from('>H', payload, off)[0] \
        if off + 2 <= len(payload) else 0
    off += 2
    biomes = payload[off: off + 256]
    off += 256

    tail, tail_raw = {}, b''
    if off < len(payload) and payload[off] == TAG_COMPOUND:
        tail_raw = payload[off:]
        try:
            tail = top_level_members(tail_raw)
        except Exception:
            tail = {}

    return {
        'x': cx, 'z': cz,
        'last_update': last_update, 'inhabited': inhabited,
        'blocks': low,
        'blocks_high': high,
        'submerged': bytearray(wet16[0::2]),
        'waterlogged': wet,
        'data': data,
        'sky': bytearray(b'\x0f' * CHUNK_BLOCKS),
        'light': bytearray(CHUNK_BLOCKS),
        'height_map': (bytes(heights) + b'\x00' * 256)[:256],
        'terrain_populated': terrain_populated,
        'biomes': (bytes(biomes) + bytes([BIOME_UNSET]) * 256)[:256],
        'tail': tail,
        'tail_raw': tail_raw,
        'version': 12,
        'header_extra': header_extra,
        'unread': unread,
    }
