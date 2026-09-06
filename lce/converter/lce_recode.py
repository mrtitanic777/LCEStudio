"""
Re-encoding console chunks from one storage format into another.

Retargeting a world inside TU17 - TU68 only has to change a version word,
because every one of those title updates stores chunks the same way. Crossing
out of that family - TU75 down to TU19, or TU19 down to TU11 - does not, and
that is what this file is for.

The shape of it is one intermediate and two halves:

    decode  (lce_java)      any format  ->  flat 256-tall arrays
    encode  (here)          flat arrays ->  any format

lce_java already decodes every format the game ever wrote, and does it against
ground truth: a tile entity records absolute coordinates AND implies a block
id, so a decoded chunk can be checked block by block rather than eyeballed.
This file is the other direction, verified the same way - encode, decode the
result, and require it to match what went in.

Every format the game ever wrote can now be both read and written, so a world
can move between any two title updates, TU0 to TU75, in either direction.

What this deliberately does NOT do is invent blocks. Going backwards, a world
can hold blocks the older format has no room for - v12 stores a 12-bit id and
everything before it has eight - and the only honest answers are to substitute
something that existed or to leave air. Both lose something, so it is the
caller's decision through downgrade_blocks, and the count always comes back.
"""

import struct
from pathlib import Path

from . import lce_java as lj

from ._res import base as _res_base       # vendored: frozen-aware resource base
_HERE = _res_base()

# A half chunk is 128 tall; a full one is two halves stacked.
HALF_HEIGHT = lj.HALF_HEIGHT
BLOCKS_PER_HALF = lj.BLOCKS_PER_HALF
CHUNK_BLOCKS = lj.CHUNK_BLOCKS

# Data offsets are stored shifted left one bit, so an offset must be EVEN to
# survive the round trip (see desc_for_offset). Genuine chunks pack the data
# tight - offsets run 0, 10, 20, 30 with no gaps - and every payload size is
# even (10, 20, 48, 64), so tight packing keeps every offset even on its own.
# Padding to four, as an earlier version did, inserted two dead bytes into
# every small block and made the data region a different size and shape than
# the game writes.
DATA_ALIGN = 2

# The largest offset a descriptor can hold: (desc >> 1) & 0x7FFE.
MAX_DATA_OFFSET = 0x7FFE


class CannotEncode(ValueError):
    """
    A chunk that will not fit the format it is being asked to become.

    A ValueError because that is what every caller of retarget_payload
    already treats as "this cannot be done", and this is the same thing said
    about a different boundary.
    """


# ---------------------------------------------------------------------------
# Splitting a full column back into the two halves the console stores
# ---------------------------------------------------------------------------

def split_halves(flat: bytearray):
    """
    The inverse of lce_java._join_halves.

    A 256-tall column indexed xz*256 + y becomes two 128-tall halves each
    indexed xz*128 + y. The console stores them separately because the format
    predates 256-tall worlds; the upper half is simply absent in a TU11 save.
    """
    lower = bytearray(BLOCKS_PER_HALF)
    upper = bytearray(BLOCKS_PER_HALF)
    for xz in range(256):
        at = xz * 256
        lo = xz * HALF_HEIGHT
        lower[lo: lo + HALF_HEIGHT] = flat[at: at + HALF_HEIGHT]
        upper[lo: lo + HALF_HEIGHT] = flat[at + HALF_HEIGHT: at + 256]
    return lower, upper


def desc_for_offset(at: int, kind: int) -> int:
    """
    A tile storage descriptor pointing at *at*, with storage kind *kind*.

    The reader recovers the offset as (desc >> 1) & 0x7FFE, which drops the
    low bit and keeps fourteen. Inverting that puts the offset back at
    desc bits 2 - 15, and means only multiples of four survive intact: bit 1
    of the offset would land on desc bit 2, which kind 3 uses as a flag.
    """
    if at % DATA_ALIGN:
        raise CannotEncode(f"data offset {at} is not aligned to {DATA_ALIGN}")
    if at > MAX_DATA_OFFSET:
        raise CannotEncode(f"data offset {at} does not fit a descriptor")
    return ((at << 1) & 0xFFFC) | kind


# ---------------------------------------------------------------------------
# Compressed tile storage - the blocks of one half
# ---------------------------------------------------------------------------

def write_compressed_tile_storage(half: bytearray) -> bytes:
    """
    Pack half a chunk's blocks the way TU17 - TU68 store them.

    The half is cut into 512 blocks of 64 tiles, and each block is stored
    however costs least: one repeated value in the descriptor itself, a
    palette of 2, 4 or 16 with packed indices, or 64 literal bytes.

    EACH BLOCK GETS ITS OWN DATA SLOT, even when two blocks are identical. An
    earlier version shared one copy between identical blocks - it round-trips
    through our own decoder perfectly and is a third the size - but the GAME
    never does this: across 1,452 block-storage halves in genuine TU68 chunks,
    not one descriptor points at another's data. Sharing offsets makes the
    game crash on load, so the data is written straight through in block order,
    which is exactly what a genuine chunk looks like.

    Returns the storage complete with its four-byte length prefix.
    """
    descs = bytearray(1024)
    data = bytearray()

    for block in range(512):
        where = lj._TILE_INDEX[block]
        tiles = bytes(half[where[t]] for t in range(64))
        # FIRST-APPEARANCE order, not sorted. A genuine palette reads e.g.
        # [7, 87, 0, 255] - the order the values are first met scanning the
        # sub-block - not [0, 7, 87, 255]. Sorting changed the palette and
        # inverted the packed bits versus what the game writes.
        distinct = []
        for v in tiles:
            if v not in distinct:
                distinct.append(v)

        # One value throughout: it fits in the descriptor, no data at all.
        if len(distinct) == 1:
            desc = 3 | 4 | (distinct[0] << 8)
            struct.pack_into('<H', descs, block * 2, desc)
            continue

        if len(distinct) <= 16:
            kind = 0 if len(distinct) <= 2 else (1 if len(distinct) <= 4 else 2)
            bits = (1, 2, 4)[kind]
            palette_size = 1 << bits
            packed_size = 8 << kind
            shift = 3 - kind
            bit_mask = 7 >> kind
            byte_mask = 62 >> shift

            # Unused palette slots are filled with 0xFF, not 0x00 - a genuine
            # palette reads e.g. 07 57 00 ff, one real value short of full,
            # padded with ff. The game never reads an unused slot, so this is
            # only about matching the bytes the game itself writes.
            palette = bytearray(b'\xff' * palette_size)
            palette[:len(distinct)] = bytes(distinct)
            index_of = {v: i for i, v in enumerate(distinct)}
            packed = bytearray(packed_size)
            for tile in range(64):
                idx = (tile >> shift) & byte_mask
                bit = (tile & bit_mask) * bits
                packed[idx] |= index_of[tiles[tile]] << bit
            payload = bytes(palette) + bytes(packed)
        else:
            kind = 3
            payload = tiles

        while len(data) % DATA_ALIGN:
            data.append(0)
        at = len(data)
        if at > MAX_DATA_OFFSET:
            raise CannotEncode(
                "this chunk's blocks do not fit one tile storage region")
        data += payload
        struct.pack_into('<H', descs, block * 2, desc_for_offset(at, kind))

    blob = bytes(descs) + bytes(data)
    return struct.pack('>i', len(blob)) + blob


# ---------------------------------------------------------------------------
# Sparse nibble storage - data, sky light and block light
# ---------------------------------------------------------------------------

def write_sparse_nibble_storage(half: bytearray, all_fifteen: bool) -> bytes:
    """
    Pack half a chunk's nibbles the way TU17 - TU68 store them.

    One byte per y layer names a 128-byte plane, and two ids are reserved:
    128 for an all-zero layer and, for light only, 129 for all-fifteen. Those
    two cover most of a chunk - everything below ground is dark and everything
    above it is full sky - so a chunk usually needs only a handful of real
    planes.

    EACH real layer gets its OWN plane, even when two layers are identical.
    An earlier version shared one plane between identical layers - it decodes
    back perfectly and is much smaller - but the GAME does not: 767 of 4,356
    genuine nibble sections have duplicate planes in their pool. Sharing them
    makes the game crash on load, the same as sharing block data does. The two
    markers still collapse the all-zero and all-fifteen layers, because genuine
    chunks use those markers too - but nothing else is shared.

    There are 128 real ids and exactly 128 layers, so with no sharing this
    still cannot run out.
    """
    ids = bytearray(128)
    pool = bytearray()

    zero_plane = bytes(128)
    full_plane = b'\xff' * 128

    for y in range(HALF_HEIGHT):
        layer = half[y::HALF_HEIGHT]
        plane = bytes(lj.pack_nibbles(layer))
        if plane == zero_plane:
            ids[y] = 128
            continue
        if all_fifteen and plane == full_plane:
            ids[y] = 129
            continue
        pid = len(pool) // 128
        if pid > 127:
            raise CannotEncode("more than 128 nibble planes")
        pool += plane
        ids[y] = pid

    count = len(pool) // 128
    return struct.pack('>i', count) + bytes(ids) + bytes(pool)


# ---------------------------------------------------------------------------
# A whole chunk
# ---------------------------------------------------------------------------

def encode_tile_chunk(ch: dict, version: int) -> bytes:
    """
    Write a decoded chunk as TU17 - TU68 tile storage.

    *version* picks which of the four the header claims to be. The bodies are
    identical across all four - that is why retargeting inside this range
    never has to touch a block - so the only thing it changes is the version
    word and whether inhabitedTime is present.
    """
    if version not in (8, 9, 10, 11):
        raise CannotEncode(f"chunk version {version} is not tile storage")

    out = bytearray()
    out += struct.pack('>h', version)
    out += struct.pack('>i', ch['x'])
    out += struct.pack('>i', ch['z'])
    out += struct.pack('>q', ch.get('last_update', 0))
    if version >= 9:
        out += struct.pack('>q', ch.get('inhabited', 0))

    lower_b, upper_b = split_halves(ch['blocks'])
    out += write_compressed_tile_storage(lower_b)
    out += write_compressed_tile_storage(upper_b)

    for key, all_fifteen in (('data', False), ('sky', True), ('light', True)):
        lower, upper = split_halves(ch[key])
        out += write_sparse_nibble_storage(lower, all_fifteen)
        out += write_sparse_nibble_storage(upper, all_fifteen)

    height_map = bytes(ch.get('height_map') or b'')[:256]
    out += height_map.ljust(256, b'\x00')
    out += struct.pack('>H', ch.get('terrain_populated', 0))
    biomes = bytes(ch.get('biomes') or b'')[:256]
    out += biomes.ljust(256, b'\x00')
    out += ch.get('tail_raw') or b''
    return bytes(out)


# ---------------------------------------------------------------------------
# NBT chunks - TU0 to TU16
#
# The other end of the range. These have no version word at all: a chunk is a
# TAG_Compound and the LENGTH of its Blocks array is the only thing that says
# whether the world is 128 or 256 tall. That makes the two heights genuinely
# different formats sharing one container, so both are written here.
# ---------------------------------------------------------------------------

def _entry_body(entry: bytes) -> bytes:
    """A serialised NBT member, name and all - passed through unchanged."""
    return bytes(entry)


def encode_nbt_chunk(ch: dict, tall: bool = True,
                     tile_ticks: bool = True) -> bytes:
    """
    Write a decoded chunk as a pre-TU17 NBT chunk.

    *tall* picks TU12 - TU16 (256, with Biomes) over TU0 - TU11 (128, no
    Biomes). Going to a 128-tall world discards everything above y 127 -
    there is nowhere to put it, and that is the honest cost of the trip, not
    a bug. The caller decides whether to pay it.

    Blocks is a stack of 128-tall halves indexed (x*16+z)*128 + y, not one
    flat array. Getting that wrong produces a world that looks plausible and
    is transposed; it is why the decoder was checked against tile entity
    coordinates rather than by eye.
    """
    lower_b, upper_b = split_halves(ch['blocks'])
    if tall:
        blocks = bytes(lower_b) + bytes(upper_b)
    else:
        blocks = bytes(lower_b)

    def nibble_array(key):
        lower, upper = split_halves(ch[key])
        vals = bytes(lower) + bytes(upper) if tall else bytes(lower)
        return bytes(lj.pack_nibbles(vals))

    # The pre-TU5 base game reads a chunk's tags POSITIONALLY, not by name, so
    # the order has to be exactly what the game itself writes or its parser
    # walks off the end and the world crashes at "Initializing server". A title
    # update reads by name and tolerates any order, which is why TU8 loaded a
    # chunk in a different order; the base game does not. The order below is
    # taken verbatim from genuine TU0-TU4 saves.
    def named(key, default=b''):
        entry = (ch.get('tail') or {}).get(key)
        if entry:
            return _entry_body(entry)
        if key in ('Entities', 'TileEntities'):
            return lj.tag_list_of_compounds(key, [])
        return None

    blocks_t = lj.tag_byte_array('Blocks', blocks)
    lastupd = lj.tag_long('LastUpdate', ch.get('last_update', 0))
    xpos = lj.tag_int('xPos', ch['x'])
    data_t = lj.tag_byte_array('Data', nibble_array('data'))
    zpos = lj.tag_int('zPos', ch['z'])
    terrpop = lj.tag_byte('TerrainPopulated',
                          1 if ch.get('terrain_populated', 1) else 0)
    blocklight = lj.tag_byte_array('BlockLight', nibble_array('light'))
    skylight = lj.tag_byte_array('SkyLight', nibble_array('sky'))
    heightmap = lj.tag_byte_array(
        'HeightMap', bytes(ch.get('height_map') or b'').ljust(256, b'\x00')[:256])
    entities = named('Entities')
    tileents = named('TileEntities')
    tileticks = named('TileTicks') if tile_ticks else None

    if tall:
        # 256-tall (TU12-16) genuine order, with Biomes.
        biomes = lj.tag_byte_array(
            'Biomes', bytes(ch.get('biomes') or b'').ljust(256, b'\xff')[:256])
        members = [blocks_t, lastupd, xpos, data_t, zpos, terrpop,
                   blocklight, skylight, heightmap, biomes, entities, tileents]
    else:
        # 128-tall (TU0-11) genuine order.
        members = [blocks_t, lastupd, xpos, data_t, zpos, terrpop,
                   blocklight, skylight, heightmap, entities, tileents]
    if tileticks is not None:
        members.append(tileticks)

    return lj.tag_compound('', lj.tag_compound('Level', *members))


# ---------------------------------------------------------------------------
# Going backwards: blocks the older format cannot hold
#
# TU69 - TU75 store a block as 16 bits - (id << 4) | data - so an id can be
# twelve bits wide. Every earlier format stores the id in one byte. Anything
# above 255 therefore has no representation at all in the target, and there
# are only two honest answers: put something there that did exist, or leave
# air. Both lose something, so the caller picks, and either way the count
# comes back so it can be reported rather than discovered later in-game.
#
# Measured across 7,620 v12 chunks in seven TU69 - TU75 worlds: 182,756 of
# 499,384,320 blocks, 0.037%, carry an id over 255. So this affects real
# worlds, but thinly - and it is the only thing lost going down, because
# every other part of the chunk survives the trip byte for byte.
# ---------------------------------------------------------------------------

# What an id that cannot be represented becomes. Air is the default because it
# is the one substitution that can never place a block somewhere it could not
# have been - a wrong solid block changes the world, a hole is at least
# obviously a hole.
SUBSTITUTE_AIR = 0


def downgrade_blocks(ch: dict, substitute: int = SUBSTITUTE_AIR,
                     target_tu: int = None, translate: bool = True):
    """
    Make a chunk fit what the target can hold, and say how much did not.

    The order here is the whole trick. A TU69 block id is TWELVE bits, and
    the substitution table is keyed on the full id - so the table has to be
    consulted BEFORE anything is collapsed to eight bits. Doing it the other
    way round looks at the low byte of a coral block, sees id 3, matches
    nothing, and turns the entire ocean into air.

    Three outcomes per block:

      * a rule exists - it becomes that, and counts as swapped. Nothing is
        lost: a stripped spruce log becomes a spruce log.
      * no rule and the id fits in a byte - it is left alone.
      * no rule and the id does not fit - it becomes *substitute*, air by
        default, and counts as lost. Coral fans and kelp end up here, because
        there genuinely is no older block that is a coral fan.

    *translate* False turns off the first outcome: a block the target is too
    old to have becomes air instead of its nearest older equivalent. That is
    the "just convert the format, delete anything that does not belong"
    behaviour - no block ever becomes a DIFFERENT block, so the only variable
    left is the storage format itself.

    Returns (blocks, data, replaced, swapped).
    """
    rules = substitutions_for(target_tu) if target_tu is not None else None
    exact = rules['exact'] if rules else {}
    wildcard = rules['any'] if rules else {}
    high = ch.get('blocks_high')
    needs_high = bool(high) and any(high)
    if not needs_high and not exact and not wildcard:
        return ch['blocks'], ch['data'], 0, 0

    blocks = bytearray(ch['blocks'])
    data = bytearray(ch['data'])
    replaced = swapped = 0

    for n in range(len(blocks)):
        # The id as the source actually stores it, all twelve bits of it.
        bid = ((high[n] << 8) | blocks[n]) if needs_high else blocks[n]
        dat = data[n]

        hit = exact.get((bid, dat))
        if hit is None:
            hit = wildcard.get(bid)
        if hit is not None:
            # A rule means this block does not exist in the target. Translate
            # it to the nearest older block, or - when translation is off -
            # just delete it.
            if not translate:
                blocks[n], data[n] = substitute, 0
                replaced += 1
                continue
            to_id, to_data = hit
            if (to_id, to_data) != (bid, dat):
                blocks[n], data[n] = to_id & 0xFF, to_data
                swapped += 1
            continue

        if bid > 0xFF:
            # Nothing older resembles it and it will not fit a byte.
            blocks[n], data[n] = substitute, 0
            replaced += 1

    return blocks, data, replaced, swapped


# --------------------------------------------------------------------------
# Entities: a chunk carries mob and object ids as strings, and a downgrade has
# to (a) rename them to the target era's spelling - CamelCase up to TU53,
# minecraft:snake_case from TU54 - and (b) DROP any the target build never had,
# or the game crashes trying to spawn a horse that does not exist yet. Both
# come from genuine saves: templates/entity_compat.json pairs each minecraft:
# id with its CamelCase form and the first title update it was ever seen in.
# --------------------------------------------------------------------------
ENTITY_NAMESPACE_TU = 54          # ids go bare CamelCase -> minecraft: here
_ENTITY_COMPAT = None


def _entity_compat() -> dict:
    """{minecraft_id: {'camel': str, 'first_tu': int}}, loaded once."""
    global _ENTITY_COMPAT
    if _ENTITY_COMPAT is None:
        import json
        try:
            _ENTITY_COMPAT = json.loads(
                (_HERE / 'templates' / 'entity_compat.json').read_text('utf-8'))
        except Exception:
            _ENTITY_COMPAT = {}
    return _ENTITY_COMPAT


def _compound_id(comp: bytes):
    """The 'id' TAG_String value in a bare compound body, or None."""
    i = 0
    while i < len(comp):
        t = comp[i]
        if t == 0:                                  # TAG_END
            return None
        nlen = struct.unpack_from('>H', comp, i + 1)[0]
        name = comp[i + 3: i + 3 + nlen]
        pay = i + 3 + nlen
        if t == 8 and name == b'id':
            vlen = struct.unpack_from('>H', comp, pay)[0]
            return comp[pay + 2: pay + 2 + vlen].decode('latin1'), pay
        i = lj._skip(comp, pay, t)
    return None


def _retarget_entity(comp: bytes, target_tu: int):
    """A bare entity compound retargeted to *target_tu*, or None to drop it."""
    found = _compound_id(comp)
    if not found:
        return comp                                 # no id - leave it alone
    idval, pay = found
    old_namespaced = idval.startswith('minecraft:')
    want_namespaced = target_tu >= ENTITY_NAMESPACE_TU
    info = _entity_compat().get(idval if old_namespaced else 'minecraft:' + idval)

    # Decide the name for the target era and whether the target had it at all.
    if old_namespaced:
        first_tu = info['first_tu'] if info else 999
        if first_tu > target_tu:
            return None                             # never existed yet - drop
        if want_namespaced:
            return comp                             # already the right spelling
        if not info:
            return None                             # no CamelCase form known
        new_id = info['camel'].encode('latin1')
    else:
        # A CamelCase source (already old); only matters going UP past TU54.
        if not want_namespaced:
            return comp
        mc = next((k for k, v in _entity_compat().items()
                   if v['camel'] == idval), None)
        if not mc:
            return comp
        new_id = mc.encode('latin1')

    old_id = idval.encode('latin1')
    if new_id == old_id:
        return comp
    return (comp[:pay] + struct.pack('>H', len(new_id)) + new_id
            + comp[pay + 2 + len(old_id):])


def _downgrade_entity_member(member: bytes, target_tu: int) -> bytes:
    """Rename/drop every entity in an Entities/TileEntities TAG_List member."""
    if not member or member[0] != 9:                # TAG_LIST
        return member
    nlen = struct.unpack_from('>H', member, 1)[0]
    name = member[3: 3 + nlen].decode('latin1')
    i = 3 + nlen
    if member[i] != 10:                             # sub-type must be COMPOUND
        return member
    count = struct.unpack_from('>i', member, i + 1)[0]
    i += 5
    kept = []
    for _ in range(count):
        end = lj._skip(member, i, 10)               # walk one compound body
        new = _retarget_entity(member[i:end], target_tu)
        if new is not None:
            kept.append(new)
        i = end
    return lj.tag_list_of_compounds(name, kept)


def _downgrade_chunk_entities(ch: dict, target_tu: int) -> dict:
    """Filter and rename the chunk tail's Entities and TileEntities."""
    tail = ch.get('tail') or {}
    if not tail:
        return ch
    new_tail = dict(tail)
    changed = False
    for key in ('Entities', 'TileEntities'):
        member = tail.get(key)
        if member:
            fixed = _downgrade_entity_member(member, target_tu)
            if fixed != member:
                new_tail[key] = fixed
                changed = True
    return dict(ch, tail=new_tail) if changed else ch


def recode_chunk(raw: bytes, target_version, tall: bool = True,
                 substitute: int = SUBSTITUTE_AIR, target_tu: int = None,
                 translate: bool = True, tile_ticks: bool = True):
    """
    Rewrite one chunk in another storage format.

    *target_version* is 8 - 11 for tile storage or None for a pre-TU17 NBT
    chunk, where *tall* then picks TU12 - TU16 over TU0 - TU11. *target_tu*
    drives the block substitutions, which need the actual title update rather
    than just the storage format.

    *translate* False deletes blocks the target cannot hold instead of
    replacing them with a nearest match - a straight format conversion.

    Returns (bytes, replaced, swapped) - what was lost and what was only
    changed. Neither happens quietly.
    """
    ch = lj.decode_chunk(raw)

    # Rename mob/object ids to the target era's spelling and drop any the target
    # build never had - a horse in a TU0 chunk crashes the world at spawn.
    if target_tu is not None:
        ch = _downgrade_chunk_entities(ch, target_tu)

    # v12 is the only format with room for a 12-bit id, and TU69+ is newer
    # than everything in the substitution table, so going there loses nothing.
    if target_version == 12:
        return encode_v12_chunk(ch), 0, 0

    blocks, data, replaced, swapped = downgrade_blocks(ch, substitute,
                                                       target_tu, translate)
    ch = dict(ch, blocks=blocks, data=data)
    if target_version is None:
        return (encode_nbt_chunk(ch, tall=tall, tile_ticks=tile_ticks),
                replaced, swapped)
    return encode_tile_chunk(ch, target_version), replaced, swapped


def target_for_title_update(tu: int):
    """
    (chunk version or None, tall) for a title update.

    None means a pre-TU17 NBT chunk; *tall* then says whether it is the
    256-tall TU12 - TU16 shape or the 128-tall TU0 - TU11 one.
    """
    if tu is None:
        raise CannotEncode("no title update given")
    if tu <= 11:
        return None, False
    if tu <= 16:
        return None, True
    if tu <= 18:
        return 8, True
    if tu <= 30:
        return 9, True
    if tu <= 59:
        return 10, True
    if tu <= 68:
        return 11, True
    if tu <= 75:
        return 12, True
    raise CannotEncode(f"TU{tu} is past TU75, the last title update there is.")


# ---------------------------------------------------------------------------
# Sectioned tile storage v12 - TU69 to TU75
#
# The last format, and the only one that stores a block as 16 bits:
# (id << 4) | data, so the id is twelve bits and the metadata rides in the low
# nibble. That is why it needed twice the space, and why it is the only format
# that can hold everything the others can.
#
# A chunk is 16 sections stacked; each section is 64 sub-blocks of 4x4x4, each
# with a 2-byte descriptor naming a SELECTOR - how those 64 values are stored:
#
#     0    every value the same, and it fits in the descriptor's low byte
#     2    2-entry palette,  1 bit plane      12 bytes
#     4    4-entry palette,  2 bit planes     24
#     6    8-entry palette,  3 bit planes     40
#     8    16-entry palette, 4 bit planes     64
#    14    64 raw u16 values                 128
#    15    the same, then 128 bytes of liquid data
#
# A "bit plane" holds one bit of every index rather than packing indices side
# by side: plane p, byte j, bit 7-e is bit p of entry j*8+e. Writing it is the
# read loop inverted.
# ---------------------------------------------------------------------------

V12_HEADER = lj.V12_SECTIONS_OFF          # 76 bytes before the first section
V12_SUBBLOCKS = 64
V12_MAX_SECTION = 255 << 8                # the size byte counts 256-byte units

# selector -> (palette entries, bit planes, total bytes)
V12_PALETTE_SELECTORS = [(2, 2, 1, 12), (4, 4, 2, 24),
                         (6, 8, 3, 40), (8, 16, 4, 64)]


def _v12_gather(src, base: int) -> bytes:
    """The 64 u16 values of one sub-block, in the order the format stores them."""
    out = bytearray(128)
    for n, pos in enumerate(lj._V12_SCATTER):
        out[n * 2] = src[base + pos]
        out[n * 2 + 1] = src[base + pos + 1]
    return bytes(out)


def _v12_pack_subblock(values: bytes, wet):
    """
    One sub-block's payload and selector, chosen to be as small as it can be.

    *values* is 128 bytes of u16; *wet* is its liquid data or None. Returns
    (selector, payload); an empty payload means the descriptor carries it all.
    """
    entries = [values[n * 2: n * 2 + 2] for n in range(V12_SUBBLOCKS)]
    distinct = sorted(set(entries))

    if not wet:
        # One value throughout, and small enough to live in the descriptor.
        if len(distinct) == 1 and distinct[0][1] == 0:
            return 0, b''
        for selector, size, planes, total in V12_PALETTE_SELECTORS:
            if len(distinct) > size:
                continue
            palette = bytearray(size * 2)
            for i, value in enumerate(distinct):
                palette[i * 2: i * 2 + 2] = value
            index_of = {v: i for i, v in enumerate(distinct)}
            body = bytearray(planes * 8)
            for n, value in enumerate(entries):
                idx = index_of[value]
                j, e = n // 8, n % 8
                for p in range(planes):
                    if idx >> p & 1:
                        body[p * 8 + j] |= 0x80 >> e
            payload = bytes(palette) + bytes(body)
            if len(payload) != total:
                raise CannotEncode(f"selector {selector} payload is "
                                   f"{len(payload)} bytes, not {total}")
            return selector, payload
        return 14, bytes(values)
    return 15, bytes(values) + bytes(wet)


def encode_v12_chunk(ch: dict) -> bytes:
    """
    Write a decoded chunk as TU69 - TU75 sectioned tile storage.

    Blocks, block data, height map, biomes, entities and tile entities all
    round trip exactly. Two things are written on inference rather than
    measurement, and are called out because nothing here can prove them:

      * the 31 header bytes between the pool size and the section sizes have
        no known meaning. A chunk that came from v12 gets its own back; one
        from anywhere else gets zeroes.
      * the four light groups after the section pool are written in the shape
        the pre-TU17 format uses, which is what the reader's own arithmetic
        implies, in the order sky-lower, sky-upper, light-lower, light-upper.
        The decoder skips them, so a round trip can only check that the sizes
        add up, not that the order is right.
    """
    blocks = ch['blocks']
    high = ch.get('blocks_high') or bytes(CHUNK_BLOCKS)
    data = ch['data']
    submerged = ch.get('submerged')

    # Back to the 16-bit form the format stores: (id << 4) | data.
    raw16 = bytearray(CHUNK_BLOCKS * 2)
    for n in range(CHUNK_BLOCKS):
        value = (((high[n] << 8) | blocks[n]) << 4) | (data[n] & 0x0F)
        raw16[n * 2] = value & 0xFF
        raw16[n * 2 + 1] = value >> 8
    wet16 = None
    if submerged and any(submerged):
        wet16 = bytearray(CHUNK_BLOCKS * 2)
        wet16[0::2] = submerged

    sections, sizes = [], bytearray(16)
    for sec in range(16):
        descs = bytearray(128)
        body = bytearray()
        seen = {}
        at = 0
        for i in range(4):
            for j in range(4):
                for k in range(4):
                    base = sec * 32 + k * 8 + j * 2048 + i * 32768
                    values = _v12_gather(raw16, base)
                    wet = _v12_gather(wet16, base) if wet16 else None
                    if wet and not any(wet):
                        wet = None
                    selector, payload = _v12_pack_subblock(values, wet)

                    if selector == 0:
                        descs[at], descs[at + 1] = values[0], 0
                        at += 2
                        continue

                    start = seen.get(payload)
                    if start is None:
                        start = len(body)
                        if start > 0x3FFC:
                            raise CannotEncode(
                                "a v12 section's sub-blocks do not fit its "
                                "16 KB addressing")
                        body += payload
                        seen[payload] = start
                    # start = lo * 4 + ((hi & 15) << 10), so it is a multiple
                    # of four and fourteen bits wide.
                    descs[at] = (start >> 2) & 0xFF
                    descs[at + 1] = (selector << 4) | ((start >> 10) & 0x0F)
                    at += 2

        section = bytes(descs) + bytes(body)
        # Section sizes are counted in 256-byte units, so pad up to one.
        if len(section) % 256:
            section += bytes(256 - len(section) % 256)
        if len(section) > V12_MAX_SECTION:
            raise CannotEncode(f"section {sec} is {len(section):,} bytes, "
                               f"more than the size byte can count")
        sizes[sec] = len(section) // 256
        sections.append(section)

    pool = sum(len(s) for s in sections)
    out = bytearray()
    out += struct.pack('>h', 12)
    out += struct.pack('>i', ch['x'])
    out += struct.pack('>i', ch['z'])
    out += struct.pack('>q', ch.get('last_update', 0))
    out += struct.pack('>q', ch.get('inhabited', 0))
    out += bytes([(pool >> 16) & 0xFF, (pool >> 8) & 0xFF, pool & 0xFF])
    out += bytes(ch.get('header_extra') or b'').ljust(31, b'\x00')[:31]
    out += bytes(sizes)
    if len(out) != V12_HEADER:
        raise CannotEncode(f"v12 header came out {len(out)} bytes, not "
                           f"{V12_HEADER}")
    for section in sections:
        out += section

    for key in ('sky', 'light'):
        lower, upper = split_halves(ch[key])
        out += write_sparse_nibble_storage(lower, True)
        out += write_sparse_nibble_storage(upper, True)

    out += bytes(ch.get('height_map') or b'').ljust(256, b'\x00')[:256]
    out += struct.pack('>H', ch.get('terrain_populated', 0))
    out += bytes(ch.get('biomes') or b'').ljust(256, b'\xff')[:256]
    out += ch.get('tail_raw') or b''
    return bytes(out)


# ---------------------------------------------------------------------------
# Substituting blocks the target is too old to hold
#
# Deleting a block leaves a hole, and a hole is the most obvious thing in a
# world. Replacing it with something that could plausibly have been there
# leaves a world that still reads as natural, which is almost always what
# somebody moving a world backwards actually wants.
#
# The table is keyed on (id, data), not id, because most of what needs
# substituting is not a separate block id at all. Stone is id 1 and granite,
# diorite and andesite are data 1 to 6 on it - an id-only rule would either
# miss them entirely or flatten real stone along with them.
# ---------------------------------------------------------------------------

SUBSTITUTIONS_CSV = _HERE / 'templates' / 'block_substitutions.csv'

# data = -1 in the file means "whatever the data value is".
ANY_DATA = -1

_SUBSTITUTIONS = None


def load_substitutions() -> list:
    """
    [(id, data, first_tu, to_id, to_data)] from the table, or [] if it is gone.

    Read once and cached. A missing or malformed table means no substitutions
    happen, which is the old behaviour - blocks the target cannot hold become
    air - rather than an error partway through a conversion.
    """
    global _SUBSTITUTIONS
    if _SUBSTITUTIONS is not None:
        return _SUBSTITUTIONS

    rows = []
    try:
        import csv
        with SUBSTITUTIONS_CSV.open(encoding='utf-8') as f:
            for row in csv.DictReader(
                    ln for ln in f if not ln.lstrip().startswith('#')):
                try:
                    rows.append((int(row['id']), int(row['data']),
                                 int(row['first_tu']),
                                 int(row['to_id']), int(row['to_data'])))
                except (TypeError, ValueError, KeyError):
                    continue
    except OSError:
        rows = []
    _SUBSTITUTIONS = rows
    return rows


def substitutions_for(target_tu: int) -> dict:
    """
    {(id, data): (id, data)} for everything *target_tu* is too old to hold.

    An exact (id, data) match wins over an any-data rule for the same id, so a
    table can say "this whole id becomes that" and still carve out one data
    value that becomes something else.
    """
    exact, wildcard = {}, {}
    for bid, data, first_tu, to_id, to_data in load_substitutions():
        if target_tu is None or target_tu >= first_tu:
            continue
        if data == ANY_DATA:
            wildcard[bid] = (to_id, to_data)
        else:
            exact[(bid, data)] = (to_id, to_data)
    return {'exact': exact, 'any': wildcard}
