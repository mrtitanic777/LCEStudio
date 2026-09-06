"""Block id -> display colour + flags, for the FULL Legacy Console Edition block
set (ids 0-252: the Beta blocks in ``_TABLE`` plus every TU-era block in
``_LCE_EXT`` — concrete, terracotta, quartz, prismarine, purpur, shulker/glazed,
the new wood families, redstone gear, …). Names come from ``lce.names.BLOCK_NAMES``.

Blocks with a real terrain.png tile use it (``_FACE_TILES``); the rest render as a
neutral tile tinted by their curated flat colour, so a block always shows in a
faithful colour. Only a genuinely unknown id (none, in practice) falls back to a
neutral grey — NOT the old magenta 'pink wool' that TU-era blocks used to show."""

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
# truly-unknown ids (none, once _LCE_EXT below is applied) get a neutral grey, NOT
# a garish magenta — the old magenta was the "pink wool" seen on any TU-era block.
_DEFAULT = ("unknown", (140, 140, 146), True)

# 16 dye colours (Minecraft order 0..15), for the per-colour block families below.
_DYE = [
    (221, 223, 225), (219, 125, 62), (179, 80, 188), (107, 138, 201),   # white orange magenta lightblue
    (203, 192, 63),  (128, 179, 53), (211, 128, 159), (86, 91, 91),     # yellow lime pink gray
    (159, 165, 165), (58, 121, 140), (130, 66, 176),  (49, 66, 156),    # lightgray cyan purple blue
    (128, 84, 51),   (77, 92, 39),   (162, 66, 60),   (37, 40, 45),     # brown green red black
]


def _shulker(i):    # 219..234 white..black
    return _DYE[i - 219], True


def _glazed(i):     # 235..250 white..black (glazed terracotta – lighter/patterned)
    r, g, b = _DYE[i - 235]
    return ((r + 235) // 2, (g + 235) // 2, (b + 235) // 2), True


# id -> ((r,g,b), opaque_solid) for every LCE (TU-era) block not in _TABLE above.
# Names come from lce.names.BLOCK_NAMES; colours are faithful top-face tints.
_LCE_EXT = {
    21: ((94, 108, 140), True),   22: ((38, 67, 137), True),    23: ((110, 110, 110), True),
    25: ((110, 78, 52), True),    27: ((150, 120, 70), False),  28: ((150, 120, 70), False),
    29: ((150, 145, 100), True),  30: ((235, 235, 235), False), 32: ((150, 118, 66), True),
    33: ((150, 140, 110), True),  34: ((160, 150, 120), True),  36: ((150, 140, 110), True),
    70: ((127, 127, 127), False), 72: ((156, 127, 78), False),  84: ((110, 78, 60), True),
    95: ((236, 236, 236), False), 97: ((127, 127, 127), True),  98: ((122, 122, 122), True),
    99: ((150, 110, 85), True),   100: ((188, 74, 68), True),   101: ((140, 140, 140), False),
    102: ((200, 226, 233), False), 103: ((110, 140, 45), True), 104: ((110, 150, 40), False),
    105: ((110, 150, 40), False), 106: ((60, 120, 40), False),  107: ((156, 127, 78), False),
    108: ((150, 90, 75), True),   109: ((122, 122, 122), True), 110: ((110, 95, 110), True),
    111: ((40, 110, 40), False),  112: ((44, 22, 26), True),    113: ((44, 22, 26), False),
    114: ((44, 22, 26), True),    115: ((140, 30, 35), False),  116: ((90, 50, 60), True),
    117: ((120, 100, 80), False), 118: ((70, 70, 70), True),    119: ((14, 12, 26), True),
    120: ((90, 110, 90), True),   121: ((218, 224, 158), True), 122: ((25, 20, 35), True),
    123: ((120, 90, 55), True),   124: ((205, 165, 95), True),  125: ((156, 127, 78), True),
    126: ((156, 127, 78), False), 127: ((130, 80, 40), True),   128: ((216, 203, 143), True),
    129: ((110, 140, 110), True), 130: ((30, 45, 45), True),    131: ((150, 130, 90), False),
    132: ((150, 130, 90), False), 133: ((42, 203, 111), True),  134: ((103, 80, 50), True),
    135: ((192, 175, 121), True), 136: ((160, 115, 80), True),  138: ((92, 200, 196), False),
    139: ((122, 122, 122), False), 140: ((120, 70, 55), False), 141: ((70, 140, 40), False),
    142: ((70, 140, 40), False),  143: ((156, 127, 78), False), 144: ((200, 200, 190), False),
    145: ((70, 70, 70), True),    146: ((160, 118, 56), True),  147: ((230, 200, 90), False),
    148: ((200, 200, 200), False), 149: ((150, 150, 150), False), 150: ((170, 140, 120), False),
    151: ((120, 110, 90), False), 152: ((170, 30, 20), True),   153: ((120, 90, 85), True),
    154: ((70, 70, 70), False),   155: ((235, 231, 224), True), 156: ((235, 231, 224), True),
    157: ((150, 120, 70), False), 158: ((110, 110, 110), True), 159: ((209, 178, 161), True),
    160: ((236, 236, 236), False), 161: ((96, 123, 54), False), 162: ((120, 110, 95), True),
    163: ((168, 90, 50), True),   164: ((66, 43, 20), True),    165: ((110, 190, 90), False),
    166: ((190, 40, 40), False),  167: ((180, 180, 180), False), 168: ((90, 140, 130), True),
    169: ((190, 210, 205), True), 170: ((200, 175, 40), True),  171: ((221, 223, 225), False),
    172: ((150, 95, 66), True),   173: ((25, 25, 25), True),    174: ((140, 180, 235), True),
    175: ((200, 180, 40), False), 176: ((160, 120, 80), False), 177: ((160, 120, 80), False),
    178: ((120, 110, 90), False), 179: ((180, 80, 30), True),   180: ((180, 80, 30), True),
    181: ((180, 80, 30), True),   182: ((180, 80, 30), False),  183: ((103, 80, 50), False),
    184: ((192, 175, 121), False), 185: ((160, 115, 80), False), 186: ((66, 43, 20), False),
    187: ((168, 90, 50), False),  188: ((103, 80, 50), False),  189: ((192, 175, 121), False),
    190: ((160, 115, 80), False), 191: ((66, 43, 20), False),   192: ((168, 90, 50), False),
    193: ((103, 80, 50), False),  194: ((192, 175, 121), False), 195: ((160, 115, 80), False),
    196: ((168, 90, 50), False),  197: ((66, 43, 20), False),   198: ((230, 225, 215), False),
    199: ((100, 70, 100), True),  200: ((150, 120, 150), False), 201: ((170, 110, 170), True),
    202: ((172, 115, 172), True), 203: ((170, 110, 170), True), 204: ((170, 110, 170), True),
    205: ((170, 110, 170), False), 206: ((218, 224, 158), True), 207: ((100, 140, 50), False),
    208: ((120, 100, 60), True),  209: ((10, 10, 20), True),    212: ((150, 190, 240), False),
    213: ((140, 55, 30), True),   214: ((110, 20, 25), True),   215: ((90, 20, 25), True),
    216: ((200, 195, 165), True),  218: ((100, 100, 100), True),
    251: ((207, 213, 214), True), 252: ((222, 224, 215), True),
}
for _sid in range(219, 235):
    _LCE_EXT[_sid] = _shulker(_sid)
for _gid in range(235, 251):
    _LCE_EXT[_gid] = _glazed(_gid)

try:                                            # authoritative LCE block names
    from ..names import BLOCK_NAMES as _BLOCK_NAMES
except Exception:
    _BLOCK_NAMES = {}

_MAXID = 256
NAMES = [""] * _MAXID
COLORS = [(0, 0, 0)] * _MAXID
SOLID = [False] * _MAXID          # OPAQUE: full cube that hides neighbour faces
for _i in range(_MAXID):
    if _i in _TABLE:
        name, col, solid = _TABLE[_i]
    elif _i in _LCE_EXT:
        col, solid = _LCE_EXT[_i]
        name = (_BLOCK_NAMES.get(_i) or _DEFAULT[0]).lower()
    elif _i == 0:
        name, col, solid = _TABLE[0]
    else:                          # named by LCE but no curated colour, or truly unknown
        name = (_BLOCK_NAMES.get(_i) or _DEFAULT[0]).lower()
        col, solid = _DEFAULT[1], _DEFAULT[2]
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
# TU-era plants/crops that should draw as a cross, not a full cube
for _i in (104, 105, 106, 111, 115, 141, 142, 175, 200, 207): _CAT[_i] = CROSS
_CAT[59] = CROPS
for _i in (50, 75, 76): _CAT[_i] = TORCH
_CAT[65] = LADDER
for _i in (27, 28, 66, 55): _CAT[_i] = RAIL                          # rails, redstone wire~
for _i in (26, 44, 53, 60, 63, 64, 67, 68, 69, 70, 71, 72, 77, 78,
           81, 85, 92, 93, 94, 96): _CAT[_i] = BOX
# TU-era single slabs / carpet / thin blocks -> BOX (half/thin shapes below);
# the DOUBLE slabs (125,181,204) stay full CUBEs.
for _i in (126, 182, 205, 171, 147, 148, 167): _CAT[_i] = BOX
# ALL stairs (Beta + TU-era) -> BOX, oriented by metadata via stairs_boxes
STAIR_IDS = (53, 67, 108, 109, 114, 128, 134, 135, 136, 156, 163, 164, 180, 203)
for _i in STAIR_IDS: _CAT[_i] = BOX
SHAPE_CAT = [_CAT.get(_i, CUBE) for _i in range(_MAXID)]

# mutual same-type face culling (glass/leaves/ice/water/lava/slab): a face
# between two blocks in the same group is skipped (Block.shouldSideBeRendered
# overrides). group 0 = none.
_GRP = {20: 1, 18: 2, 79: 3, 8: 4, 9: 4, 10: 5, 11: 5, 44: 6, 43: 6}
for _i in (95, 102, 160): _GRP[_i] = 1        # stained/plain glass + panes cull like glass
_GRP[161] = 2                                 # acacia leaves cull like leaves
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
    63: [(0.5-_S, 0, 0.5-_S, 0.5+_S, 9*_S, 0.5+_S),        # standing sign: thin post…
         (_S, 9*_S, 7*_S, 1-_S, 1, 9*_S)],                # …+ a board panel on top
    68: [(0, 4.5*_S, 0, 1, 12.5*_S, 2*_S)],           # wall sign: a board panel on the -Z wall
    26: [(0, 0, 0, 1, 9*_S, 1)],                      # bed (approx)
    64: [(0, 0, 0, 3*_S, 1, 1)],                      # door (approx, -X face)
    71: [(0, 0, 0, 3*_S, 1, 1)],
    126: [(0, 0, 0, 1, 0.5, 1)],                      # wooden slab (bottom half)
    182: [(0, 0, 0, 1, 0.5, 1)],                      # red sandstone slab
    205: [(0, 0, 0, 1, 0.5, 1)],                      # purpur slab
    171: [(0, 0, 0, 1, _S, 1)],                       # carpet (1/16 thin)
    147: [(_S, 0, _S, 1-_S, _S, 1-_S)],               # light weighted pressure plate
    148: [(_S, 0, _S, 1-_S, _S, 1-_S)],               # heavy weighted pressure plate
    167: [(0, 0, 0, 1, 3*_S, 1)],                     # iron trapdoor (closed, floor)
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


# ---- metadata-driven ORIENTED boxes (wall signs, standing signs, trapdoors, doors) --
# LCE stores the same block metadata as Java pre-1.13, so these follow the classic
# RenderBlocks encodings. north=-Z, south=+Z, west=-X, east=+X.
def wall_sign_boxes(meta):
    """Thin board hung flush on the wall the sign faces (meta 2/3/4/5 = N/S/W/E)."""
    y0, y1, th = 4.5 * _S, 12.5 * _S, 2 * _S
    m = meta & 7
    if m == 2:   return [(0, y0, 0, 1, y1, th)]           # facing north  -> board on -Z
    if m == 3:   return [(0, y0, 1 - th, 1, y1, 1)]       # facing south  -> board on +Z
    if m == 4:   return [(0, y0, 0, th, y1, 1)]           # facing west   -> board on -X
    if m == 5:   return [(1 - th, y0, 0, 1, y1, 1)]       # facing east   -> board on +X
    return [(0, y0, 0, 1, y1, th)]


def sign_post_boxes(meta):
    """A standing sign: centre post + a board panel perpendicular to its facing.
    16 rotations snapped to the nearest cardinal (text itself is a billboard label)."""
    post = (0.5 - _S, 0, 0.5 - _S, 0.5 + _S, 9 * _S, 0.5 + _S)
    d = int(round((meta & 15) / 4.0)) & 3                 # 0=S(+Z) 1=W(-X) 2=N(-Z) 3=E(+X)
    y0, y1 = 8 * _S, 15 * _S
    if d in (0, 2):                                       # facing ±Z -> board spans X, thin in Z
        board = (_S, y0, 7 * _S, 1 - _S, y1, 9 * _S)
    else:                                                 # facing ±X -> board spans Z, thin in X
        board = (7 * _S, y0, _S, 9 * _S, y1, 1 - _S)
    return [post, board]


def trapdoor_boxes(meta):
    """Trapdoor: closed -> horizontal 3/16 slab at bottom or top (0x8); open -> a
    vertical 3/16 panel against the hinge wall (0x3 = S/N/E/W)."""
    th = 3 * _S
    if meta & 4:                                          # open -> vertical on the hinge wall
        side = meta & 3
        if side == 0:   return [(0, 0, 1 - th, 1, 1, 1)]  # south (+Z)
        if side == 1:   return [(0, 0, 0, 1, 1, th)]      # north (-Z)
        if side == 2:   return [(1 - th, 0, 0, 1, 1, 1)]  # east  (+X)
        return [(0, 0, 0, th, 1, 1)]                      # west  (-X)
    if meta & 8:        return [(0, 1 - th, 0, 1, 1, 1)]  # closed, top half
    return [(0, 0, 0, 1, th, 1)]                          # closed, bottom half


def door_boxes(meta, meta_below=0):
    """A door half as a 3/16 panel on one edge. Facing comes from the LOWER half's
    metadata (`meta` for the lower voxel, `meta_below` for the upper voxel, which only
    stores the hinge bit). open (0x4) swings the panel to the adjacent edge."""
    low = meta if not (meta & 8) else meta_below          # facing lives on the lower half
    f = low & 3
    open_ = bool(low & 4)
    th = 3 * _S
    #     facing f -> closed edge;  when open, rotate to the next edge clockwise
    edge = (f + (1 if open_ else 0)) & 3
    if edge == 0:   return [(0, 0, 0, th, 1, 1)]          # -X
    if edge == 1:   return [(0, 0, 0, 1, 1, th)]          # -Z
    if edge == 2:   return [(1 - th, 0, 0, 1, 1, 1)]      # +X
    return [(0, 0, 1 - th, 1, 1, 1)]                      # +Z


def is_solid(block_id):
    return SOLID[block_id]

def color(block_id):
    return COLORS[block_id]

def name(block_id):
    return NAMES[block_id]


def is_solid(block_id):
    return SOLID[block_id]

def color(block_id):
    return COLORS[block_id]

def name(block_id):
    return NAMES[block_id]
