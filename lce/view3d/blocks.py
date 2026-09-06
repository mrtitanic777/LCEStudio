"""Block id -> display colour + flags, for the Alpha/Beta-era block set.

Colours are approximate top-of-block tints; refine later or swap for real
textures. Unknown ids fall back to a magenta so they stand out."""

# id: (name, (r,g,b), solid_opaque)
_TABLE = {
    0:  ("air",            (0, 0, 0),        False),
    1:  ("stone",          (127, 127, 127),  True),
    2:  ("grass",          (95, 159, 53),    True),
    3:  ("dirt",           (134, 96, 67),    True),
    4:  ("cobblestone",    (122, 122, 122),  True),
    5:  ("planks",         (156, 127, 78),   True),
    6:  ("sapling",        (71, 108, 40),    False),
    7:  ("bedrock",        (51, 51, 51),     True),
    8:  ("water",          (63, 118, 228),   False),
    9:  ("water",          (63, 118, 228),   False),
    10: ("lava",           (222, 110, 26),   True),
    11: ("lava",           (222, 110, 26),   True),
    12: ("sand",           (219, 207, 142),  True),
    13: ("gravel",         (136, 126, 125),  True),
    14: ("gold ore",       (143, 140, 125),  True),
    15: ("iron ore",       (136, 130, 127),  True),
    16: ("coal ore",       (105, 105, 105),  True),
    17: ("log",            (102, 81, 51),    True),
    18: ("leaves",         (60, 143, 60),    False),
    19: ("sponge",         (197, 197, 74),   True),
    20: ("glass",          (200, 226, 233),  False),
    24: ("sandstone",      (216, 203, 143),  True),
    26: ("bed",            (170, 44, 55),    False),
    31: ("tall grass",     (89, 138, 51),    False),
    35: ("wool",           (222, 222, 222),  True),
    37: ("dandelion",      (200, 200, 30),   False),
    38: ("rose",           (180, 40, 30),    False),
    39: ("brown mushroom", (150, 110, 85),   False),
    40: ("red mushroom",   (200, 60, 55),    False),
    41: ("gold block",     (250, 225, 80),   True),
    42: ("iron block",     (220, 220, 220),  True),
    43: ("double slab",    (160, 160, 160),  True),
    44: ("slab",           (160, 160, 160),  False),
    45: ("bricks",         (150, 90, 75),    True),
    46: ("tnt",            (180, 60, 50),    True),
    47: ("bookshelf",      (150, 120, 75),   True),
    48: ("mossy cobble",   (100, 120, 100),  True),
    49: ("obsidian",       (30, 26, 42),     True),
    50: ("torch",          (255, 200, 90),   False),
    51: ("fire",           (230, 140, 40),   False),
    52: ("spawner",        (40, 60, 80),     False),
    53: ("wood stairs",    (156, 127, 78),   True),
    54: ("chest",          (160, 118, 56),   True),
    55: ("redstone",       (170, 30, 20),    False),
    56: ("diamond ore",    (110, 150, 150),  True),
    57: ("diamond block",  (110, 220, 215),  True),
    58: ("crafting table", (140, 100, 60),   True),
    59: ("crops",          (110, 150, 40),   False),
    60: ("farmland",       (95, 65, 40),     True),
    61: ("furnace",        (110, 110, 110),  True),
    62: ("furnace lit",    (120, 110, 100),  True),
    63: ("sign",           (156, 127, 78),   False),
    64: ("door",           (140, 110, 70),   False),
    65: ("ladder",         (140, 115, 70),   False),
    66: ("rail",           (150, 130, 90),   False),
    67: ("cobble stairs",  (122, 122, 122),  True),
    68: ("wall sign",      (156, 127, 78),   False),
    69: ("lever",          (110, 90, 60),    False),
    71: ("iron door",      (200, 200, 200),  False),
    73: ("redstone ore",   (140, 90, 90),    True),
    74: ("redstone ore",   (150, 80, 80),    True),
    75: ("redstone torch", (150, 40, 30),    False),
    76: ("redstone torch", (200, 60, 40),    False),
    77: ("button",         (120, 120, 120),  False),
    78: ("snow",           (240, 250, 250),  False),
    79: ("ice",            (140, 180, 235),  False),
    80: ("snow block",     (245, 250, 250),  True),
    81: ("cactus",         (80, 130, 60),    True),
    82: ("clay",           (160, 165, 180),  True),
    83: ("sugar cane",     (130, 180, 100),  False),
    85: ("fence",          (156, 127, 78),   False),
    86: ("pumpkin",        (200, 130, 40),   True),
    87: ("netherrack",     (110, 55, 55),    True),
    88: ("soul sand",      (85, 68, 56),     True),
    89: ("glowstone",      (230, 200, 120),  True),
    90: ("portal",         (120, 60, 180),   False),
    91: ("jack-o-lantern", (220, 150, 50),   True),
    92: ("cake",           (240, 230, 220),  False),
    93: ("repeater",       (150, 150, 150),  False),
    94: ("repeater",       (150, 150, 150),  False),
    96: ("trapdoor",       (140, 110, 70),   False),
}
_DEFAULT = ("unknown", (230, 60, 220), True)

_MAXID = 256
NAMES = [""] * _MAXID
COLORS = [(0, 0, 0)] * _MAXID
SOLID = [False] * _MAXID          # OPAQUE: full cube that hides neighbour faces
for _i in range(_MAXID):
    name, col, solid = _TABLE.get(_i, _DEFAULT if _i else _TABLE[0])
    NAMES[_i], COLORS[_i], SOLID[_i] = name, col, solid

# RENDER: does the block produce any visible geometry? everything but air.
# (Non-opaque blocks — leaves, glass, torches, fences, flowers … — render as
#  cubes but do NOT cull their neighbours, so you can see through/past them.)
RENDER = [i != 0 for i in range(_MAXID)]

# ---- terrain.png atlas mapping (16x16 grid; tile = row*16 + col) ------------
# per block: (top, side, bottom) tile indices.  Unmapped blocks fall back to a
# neutral tile tinted by their flat colour above.
_FACE_TILES = {
    1:  (1, 1, 1),          # stone
    2:  (0, 3, 2),          # grass: green top, grassy side, dirt bottom
    3:  (2, 2, 2),          # dirt
    4:  (16, 16, 16),       # cobblestone
    5:  (4, 4, 4),          # planks
    7:  (17, 17, 17),       # bedrock
    8:  (205, 205, 205),    # water (still) — real water tile, not fallback wool
    9:  (205, 205, 205),
    10: (237, 237, 237),    # lava (still)
    11: (237, 237, 237),
    12: (18, 18, 18),       # sand
    13: (19, 19, 19),       # gravel
    14: (32, 32, 32),       # gold ore
    15: (33, 33, 33),       # iron ore
    16: (34, 34, 34),       # coal ore
    17: (21, 20, 21),       # log: rings top/bottom, bark side
    18: (53, 53, 53),       # leaves
    19: (48, 48, 48),       # sponge
    20: (49, 49, 49),       # glass
    24: (176, 192, 208),    # sandstone top/side/bottom
    35: (64, 64, 64),       # wool (white)
    41: (23, 23, 23),       # gold block
    42: (22, 22, 22),       # iron block
    43: (6, 5, 6),          # double slab
    44: (6, 5, 6),          # slab
    45: (7, 7, 7),          # bricks
    46: (9, 8, 10),         # tnt
    47: (4, 35, 4),         # bookshelf
    48: (36, 36, 36),       # mossy cobblestone
    49: (37, 37, 37),       # obsidian
    52: (65, 65, 65),       # spawner
    53: (4, 4, 4),          # wood stairs
    54: (25, 26, 25),       # chest
    56: (50, 50, 50),       # diamond ore
    57: (24, 24, 24),       # diamond block
    58: (43, 59, 43),       # crafting table
    61: (1, 45, 1),         # furnace (front handled as side)
    62: (1, 45, 1),         # furnace lit
    67: (16, 16, 16),       # cobblestone stairs
    73: (51, 51, 51),       # redstone ore
    74: (51, 51, 51),
    78: (66, 66, 66),       # snow layer
    79: (67, 67, 67),       # ice
    80: (66, 66, 66),       # snow block
    81: (69, 70, 71),       # cactus
    82: (72, 72, 72),       # clay
    86: (102, 118, 118),    # pumpkin
    87: (103, 103, 103),    # netherrack
    88: (104, 104, 104),    # soul sand
    89: (105, 105, 105),    # glowstone
    91: (102, 118, 120),    # jack-o-lantern
    # --- non-cube blocks (single tile used for cross/box/flat geometry) ---
    6:  (15, 15, 15),       # sapling
    31: (39, 39, 39),       # tall grass / fern
    32: (55, 55, 55),       # dead bush
    37: (13, 13, 13),       # dandelion
    38: (12, 12, 12),       # rose
    39: (29, 29, 29),       # brown mushroom
    40: (28, 28, 28),       # red mushroom
    50: (80, 80, 80),       # torch
    59: (90, 90, 90),       # crops (wheat)
    65: (83, 83, 83),       # ladder
    66: (128, 128, 128),    # rail
    75: (115, 115, 115),    # redstone torch (off)
    76: (99, 99, 99),       # redstone torch (on)
    83: (73, 73, 73),       # sugar cane
    85: (4, 4, 4),          # fence (planks)
}
# blocks whose grayscale atlas tile should be foliage-green-tinted (all faces)
_FOLIAGE = {18, 31, 6}

# ---- block SHAPES (from Minecraft Beta RenderBlocks geometry) ----------------
# shape categories -> handled by the mesher
CUBE, BOX, CROSS, CROPS, TORCH, LADDER, RAIL = range(7)
_S = 1.0 / 16.0
_CAT = {}
for _i in (6, 30, 31, 32, 37, 38, 39, 40, 83, 51): _CAT[_i] = CROSS   # plants, web, fire~
_CAT[59] = CROPS
for _i in (50, 75, 76): _CAT[_i] = TORCH
_CAT[65] = LADDER
for _i in (27, 28, 66, 55): _CAT[_i] = RAIL                          # rails, redstone wire~
for _i in (26, 44, 53, 60, 63, 64, 67, 68, 69, 70, 71, 72, 77, 78,
           81, 85, 92, 93, 94, 96): _CAT[_i] = BOX
SHAPE_CAT = [_CAT.get(_i, CUBE) for _i in range(_MAXID)]

# mutual same-type face culling (glass/leaves/ice/water/lava/slab): a face
# between two blocks in the same group is skipped (Block.shouldSideBeRendered
# overrides). group 0 = none.
_GRP = {20: 1, 18: 2, 79: 3, 8: 4, 9: 4, 10: 5, 11: 5, 44: 6, 43: 6}
CULL_GROUP = [_GRP.get(_i, 0) for _i in range(_MAXID)]

# static AABB boxes for BOX blocks (metadata-independent ones). Each is a list
# of (x0,y0,z0,x1,y1,z1) in 0..1. stairs/fence are computed in the mesher.
STATIC_BOXES = {
    44: [(0, 0, 0, 1, 0.5, 1)],                       # slab (bottom half)
    78: [(0, 0, 0, 1, 2*_S, 1)],                      # snow layer
    60: [(0, 0, 0, 1, 15*_S, 1)],                     # farmland
    92: [(_S, 0, _S, 1-_S, 0.5, 1-_S)],               # cake
    70: [(_S, 0, _S, 1-_S, _S, 1-_S)],                # pressure plate
    72: [(_S, 0, _S, 1-_S, _S, 1-_S)],
    77: [(0.5-3*_S, 0.375, 1-_S, 0.5+3*_S, 0.625, 1)],  # button (approx)
    81: [(_S, 0, _S, 1-_S, 1, 1-_S)],                 # cactus (inset sides)
    96: [(0, 0, 0, 1, 3*_S, 1)],                      # trapdoor (closed, floor)
    69: [(0.5-0.25, 0, 0.5-3*_S, 0.5+0.25, 3*_S, 0.5+3*_S)],  # lever base (approx)
    93: [(0, 0, 0, 1, 2*_S, 1)],                      # repeater
    94: [(0, 0, 0, 1, 2*_S, 1)],
    63: [(0.5-2*_S, 0, 0.5-2*_S, 0.5+2*_S, 1, 0.5+2*_S)],  # sign post (approx)
    68: [(0, 4*_S, 0, 1, 12*_S, 2*_S)],               # wall sign (approx)
    26: [(0, 0, 0, 1, 9*_S, 1)],                      # bed (approx)
    64: [(0, 0, 0, 3*_S, 1, 1)],                      # door (approx, -X face)
    71: [(0, 0, 0, 3*_S, 1, 1)],
}
# torch post box + which metadata leans it to a wall
TORCH_BOX = (7*_S, 0, 7*_S, 9*_S, 10*_S, 9*_S)
_FALLBACK_TILE = 64          # white-ish wool, multiplied by the block's flat colour

# which faces get a foliage/grass tint (grayscale atlas tile -> coloured)
_GRASS_TINT = (0.49, 0.72, 0.35)
_FOLIAGE_TINT = (0.30, 0.60, 0.24)
_WATER_TINT = (0.25, 0.46, 0.90)

# TILES[id] = (top, side, bottom); TINT_TOP/TINT_SIDE/TINT_BOTTOM[id] = (r,g,b)
TILES = [(_FALLBACK_TILE,) * 3] * _MAXID
TINT = [((1.0, 1.0, 1.0),) * 3] * _MAXID
TILES = []
TINT = []
for _i in range(_MAXID):
    if _i in _FACE_TILES:
        TILES.append(_FACE_TILES[_i])
        t = (1.0, 1.0, 1.0)
        if _i == 2:          # grass: tint only the top
            TINT.append((_GRASS_TINT, t, t))
        elif _i in _FOLIAGE:  # leaves / tall grass / sapling: tint all
            TINT.append((_FOLIAGE_TINT,) * 3)
        else:
            TINT.append((t, t, t))
    else:
        # unmapped: neutral tile multiplied by the flat colour so it's still distinct
        fc = tuple(c / 255.0 for c in COLORS[_i])
        if _i in (8, 9):     # water
            fc = _WATER_TINT
        TILES.append((_FALLBACK_TILE,) * 3)
        TINT.append((fc, fc, fc))


# ---- metadata-driven textures (getBlockTextureFromSideAndMetadata) ----------
# Some blocks choose their atlas tile from metadata, not just their id — wool
# colour, log/leaves/sapling wood type, slab material. TILE_LUT[id][meta][kind]
# (kind 0=top, 1=side, 2=bottom) lets the mesher look the tile up per voxel.
# Ids with no rule just repeat their static TILES row for every metadata value.
#
# Wool matters most here: this console terrain.png orders the 16 dye colours
# DIFFERENTLY from the PC BlockCloth formula, so the table below was built by
# colour-matching each metadata's dye colour to the actual atlas tiles (verified
# black=113, light-gray=225, gray=114 …). Without it every wool draws as white
# (tile 64) and colour-built structures — like the floating MINECRAFT logo,
# which is light-gray (meta 8) fill with black (meta 15) letters — flatten into
# a blank slab.
_WOOL_TILE = [64, 210, 194, 178, 162, 146, 130, 114,
              225, 209, 193, 177, 161, 145, 129, 113]

def _meta_tile(bid, meta, kind):
    if bid == 35:                                     # wool: colour by metadata
        return _WOOL_TILE[meta & 15]
    if bid == 17:                                     # log: bark type on the sides
        if kind != 1: return 21                       # rings on top & bottom
        return 116 if meta == 1 else 117 if meta == 2 else 20  # spruce/birch/oak
    if bid == 18:                                     # leaves: spruce vs oak/birch
        return 133 if (meta & 3) == 1 else 53
    if bid in (43, 44):                               # slab / double slab material
        if meta == 1: return 208 if kind == 2 else 176 if kind == 0 else 192  # sandstone
        if meta == 2: return 4                        # wood planks
        if meta == 3: return 16                       # cobblestone
        return 6 if kind != 1 else 5                  # stone (top/bottom 6, side 5)
    if bid == 6:                                      # sapling type
        m = meta & 3
        return 63 if m == 1 else 79 if m == 2 else 15
    if bid == 31:                                     # tall grass / fern / dead shrub
        return 56 if meta == 2 else 55 if meta == 0 else 39
    if bid == 64:                                      # wooden door: upper vs lower half
        return 81 if (meta & 8) else 97
    if bid == 71:                                      # iron door: upper vs lower half
        return 82 if (meta & 8) else 98
    return TILES[bid][kind]

_META_TEX_IDS = (35, 17, 18, 43, 44, 6, 31, 64, 71)
TILE_LUT = [[list(TILES[_i]) for _m in range(16)] for _i in range(_MAXID)]
for _i in _META_TEX_IDS:
    TILE_LUT[_i] = [[_meta_tile(_i, _m, 0), _meta_tile(_i, _m, 1), _meta_tile(_i, _m, 2)]
                    for _m in range(16)]


def stairs_boxes(meta):
    """Two AABB half-boxes for a stair, by metadata (0..3 orientation)."""
    m = meta & 3
    if m == 0:   return [(0, 0, 0, 0.5, 0.5, 1), (0.5, 0, 0, 1, 1, 1)]
    if m == 1:   return [(0, 0, 0, 0.5, 1, 1), (0.5, 0, 0, 1, 0.5, 1)]
    if m == 2:   return [(0, 0, 0, 1, 0.5, 0.5), (0, 0, 0.5, 1, 1, 1)]
    return [(0, 0, 0, 1, 1, 0.5), (0, 0, 0.5, 1, 0.5, 1)]


def is_solid(block_id):
    return SOLID[block_id]

def color(block_id):
    return COLORS[block_id]

def name(block_id):
    return NAMES[block_id]
