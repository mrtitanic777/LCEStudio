"""
Minecraft LCE Converter - conversion engine

Handles all four directions:

    Xbox 360  -> Win64     (wraps the proven converter.py)
    PS3       -> Win64     (wraps the proven converter_ps3.py)
    Win64     -> Xbox 360
    Win64     -> PS3

Plus the emulator folder format used by Xenia / Nexia360: a folder holding
savegame.dat and __thumbnail.png, with no STFS container around it.

------------------------------------------------------------------------------
Formats, taken from the 4J LCE source (Minecraft.World/) and verified against
real saves.

savegame.dat (Xbox 360)
    [0x00] u32 BE  payloadLen = 8 + compressedLen
    [0x04] u32 BE  0                  <- the loader requires this to be zero
    [0x08] u32 BE  decompressedSize
    [0x0C] ...     XMemCompress / LZX data
    [4 + payloadLen] trailer, ignored by the loader

GAMEDATA (PS3) is the same payload with no container and no compression at all.

4J payload (FileHeader.h)
    [0x00] u32  fileTableOffset
    [0x04] u32  numEntries
    [0x08] i16  originalSaveVersion
    [0x0A] i16  saveVersion
    ...    file bodies ...
    [fileTableOffset] numEntries x 144: wchar_t name[64], u32 length,
                                        u32 startOffset, i64 lastModified
  Big-endian on Xbox 360 / PS3, little-endian on Win64. File *contents* are
  identical across platforms - LCE NBT is big-endian everywhere - so only these
  container fields get swapped.

Region files (.mcr)
    [0x0000] 1024 x u32  location table (sector << 8) | sectorCount
    [0x1000] 1024 x u32  timestamp table
    chunk:   [u32 compLen | flags in the top 2 bits][u32 decompLen][payload]
  Chunk payload by platform (compression.cpp):
    Xbox 360  XMemCompress / LZX
    PS3       [u32 BE rleSize][raw deflate]     (eCompressionType_PS3ZLIB)
    Win64     zlib                              (eCompressionType_ZLIBRLE)
  The bytes inside are 4J's RLE stream, identical everywhere, so we only ever
  recompress - never touch the RLE data itself.

Compression parameters (compression.cpp):
    Flags = 0, WindowSize = 128 KiB, CompressionPartitionSize = 128 KiB
------------------------------------------------------------------------------
"""

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import struct
import zlib
from pathlib import Path

from . import lce_conkey

# =============================================================================
# Constants
# =============================================================================

STFS_MAGIC          = b'CON '
STFS_BASE_OFFSET    = 0xA000
STFS_BLOCK_SIZE     = 0x1000
STFS_BLOCKS_PER_GRP = 170            # StfsDataBlocksPerHashTreeLevel[0] = 0xAA
STFS_BLOCKS_PER_L1  = 0x70E4         # StfsDataBlocksPerHashTreeLevel[1]

# Geometry constants, from Horizon's StfsVolumeExtension. Everything below
# depends on the volume descriptor's ReadOnlyFormat flag:
#     FormatShift = ReadOnly ? 0 : 1
#     BlockValues = ReadOnly ? {0xAB, 0x718F} : {0xAC, 0x723A}
# We always write a read/write package, so the flags byte must be 0 - writing
# 1 there tells a reader to use the *other* geometry and every hash lookup
# lands on the wrong block.
STFS_FORMAT_SHIFT   = 1
STFS_BLOCK_VALUES   = (0xAC, 0x723A)

# Volume descriptor at 0x379 (StfsVolumeDescriptor):
#   0x379 DescriptorLength   0x37A Version          0x37B Flags
#   0x37C DirectoryAllocationBlocks u16 LE
#   0x37E DirectoryFirstBlockNumber u24 LE
#   0x381 RootHash[20]       0x395 NumberOfTotalBlocks u32 BE
#   0x399 NumberOfFreeBlocks u32 BE
VD_LENGTH_OFF   = 0x379
VD_VERSION_OFF  = 0x37A
VD_FLAGS_OFF    = 0x37B
VD_DIRALLOC_OFF = 0x37C
VD_DIRBLOCK_OFF = 0x37E
VD_ROOTHASH_OFF = 0x381
VD_TOTAL_OFF    = 0x395
VD_FREE_OFF     = 0x399

# ContentId (0x32C) is "also the highest level hash": SHA1 over the whole
# metadata region 0x344..0xA000. Confirmed against a genuine retail save.
CONTENT_ID_OFF  = 0x32C
METADATA_OFF    = 0x344

# The console signature, from Horizon's XContentPackage.ConsolePrivateKeySign:
#
#     0x000  4      'CON '
#     0x004  0x1A8  console certificate  (con_public_key, written whole)
#     0x1AC  0x80   RSA signature over file[0x22C:0x344]
#     0x22C  0x118  the signed region - licence table through to ContentId
#
# So the trust chain runs contents -> ContentId at 0x32C -> signed region ->
# signature. Rehashing has to finish before signing starts.
CON_MAGIC        = b'CON '
CON_CERT_OFF     = 0x004
CON_CERT_SIZE    = 0x1A8
CON_SIG_OFF      = 0x1AC
CON_SIG_SIZE     = 0x80
CON_SIGNED_OFF   = 0x22C
CON_SIGNED_SIZE  = 0x118

# Who the save belongs to. The console will not list a world whose ProfileId
# is not the signed-in gamertag's, however well signed the package is - which
# is the usual reason a resigned save "does nothing".
CONSOLE_ID_OFF   = 0x36C
CONSOLE_ID_SIZE  = 5
PROFILE_ID_OFF   = 0x371
PROFILE_ID_SIZE  = 8

# PKCS#1 v1.5 DigestInfo prefix for SHA-1, as RSAPKCS1SignatureFormatter emits.
_SHA1_DIGESTINFO = bytes.fromhex('3021300906052b0e03021a05000414')

LZX_WINDOW_SIZE     = 128 * 1024
LZX_PARTITION_SIZE  = 128 * 1024
LZX_BLOCK_SIZE      = 0x8000

FILE_ENTRY_SIZE     = 144
REGION_SECT_COUNT   = 1024
SECT                = 4096

# Each chunk in a region file is prefixed with [u32 compLen][u32 decompLen],
# and the top bits of compLen are flags rather than length:
#
#   bit 31  the payload is RLE'd underneath the compression
#   bit 30  set on every chunk of a save version 11 world (TU75) and on
#           nothing earlier. What it selects is not known - the LZX stream
#           decodes the same either way - but it is definitely not length.
#
# Masking only bit 31 leaves bit 30 in the number, which turns a 2,804-byte
# chunk into a 1,073,744,628-byte one and fails every read on a TU75 world.
CHUNK_FLAG_RLE      = 0x80000000
CHUNK_FLAG_V11      = 0x40000000
CHUNK_LEN_MASK      = 0x3FFFFFFF

# How each platform compresses a region chunk. The container and the chunk
# framing are shared; only this differs.
#
#   Xbox 360      XMemCompress / LZX          big-endian container
#   PS3           zlib, raw deflate at +4     big-endian container
#   Windows LCE   zlib                        little-endian container
#   Wii U         zlib                        big-endian container
#
# Wii U is noted but NOT implemented - there is no Wii U sample here to check
# against, and the one reference read of it masks the compressed length with
# 0xFFFFFF rather than the 0x3FFFFFFF used above. Whether that is a genuinely
# narrower field or just a reader that never met a flagged chunk is exactly
# the sort of thing that needs a real save to settle, so it is left alone.
WIIU_CHUNK_LEN_MASK = 0xFFFFFF
MCR_EXT             = '.mcr'
HEADER_SIZE         = 12

# Only used when a source's own saveVersion cannot be read. TU19 and TU20 both
# write 9, measured from worlds generated on them.
DEFAULT_XBOX_SAVE_VERSION = 9
DEFAULT_PS3_SAVE_VERSION  = 6
SAVE_FILE_VERSION_COMPRESSED_CHUNK_STORAGE = 8

# ---------------------------------------------------------------------------
# World size
#
# The Win64 build offers four world sizes; a TU19 console only has the smallest
# one. Converting a bigger world therefore means cropping it, not just
# repackaging it - regions outside the console's area do not exist there.
#
#   Classic  864 x 864    54 chunks   <- the only size a TU19 console has
#   Small   1024 x 1024   64 chunks
#   Medium  3072 x 3072  192 chunks
#   Large   5120 x 5120  320 chunks
#
# Bounds below are inclusive chunk coordinates measured from a genuine retail
# Xbox 360 TU19 save: overworld 54x54 chunks, Nether and End 18x18.
CONSOLE_CHUNK_BOUNDS = {
    '':      (-27, 26),     # overworld
    'DIM-1': (-9, 8),       # Nether
    'DIM1':  (-9, 8),       # End
}
CHUNKS_PER_REGION  = 32
BLOCKS_PER_CHUNK   = 16

# level.dat describes the size in chunks per axis, and the Nether divisor.
# A TU19 save carries neither tag, which implicitly means Classic.
CONSOLE_XZSIZE     = 54
CONSOLE_HELLSCALE  = 3

# The four sizes, as (label, chunks per axis, HellScale).
#
# XZSize is in chunks; HellScale divides Overworld coordinates to get Nether
# ones. Every pair below is read straight out of a world the game itself
# generated - none are inferred. The Nether does not stay a fixed size, and
# HellScale is not proportional to the world: it goes 3, 3, 6, 8.
#
#   Classic   54 chunks /  3  ->  Nether  288 blocks
#   Small     64 chunks /  3  ->  Nether  341 blocks
#   Medium   192 chunks /  6  ->  Nether  512 blocks
#   Large    320 chunks /  8  ->  Nether  640 blocks
WORLD_SIZES = {
    'classic': ('Classic', 54, 3),
    'small':   ('Small',   64, 3),
    'medium':  ('Medium',  192, 6),
    'large':   ('Large',   320, 8),
}
EXPANDABLE = ('small', 'medium', 'large')


def size_label(key: str) -> str:
    label, chunks, _ = WORLD_SIZES[key]
    return f"{label} ({chunks * BLOCKS_PER_CHUNK} x {chunks * BLOCKS_PER_CHUNK})"


_REGION_RE = re.compile(r'^(DIM-1|DIM1/)?r\.(-?\d+)\.(-?\d+)\.mcr$', re.I)


def parse_region_name(filename: str):
    """('DIM-1'|'DIM1'|'', region_x, region_z) or None if not a region file."""
    m = _REGION_RE.match(filename)
    if not m:
        return None
    dim = (m.group(1) or '').rstrip('/')
    return dim, int(m.group(2)), int(m.group(3))


def console_bounds_for(dim: str):
    return CONSOLE_CHUNK_BOUNDS.get(dim, CONSOLE_CHUNK_BOUNDS[''])


def region_intersects_console(dim: str, rx: int, rz: int) -> bool:
    """Does any chunk of this region exist on a TU19 console?"""
    lo, hi = console_bounds_for(dim)
    for r in (rx, rz):
        if r * CHUNKS_PER_REGION > hi or r * CHUNKS_PER_REGION + 31 < lo:
            return False
    return True

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

from ._res import base as _res_base       # vendored: frozen-aware resource base
_HERE = _res_base()
OUTPUT_DIR      = Path.cwd() / 'Output'    # vendored: writable default (the bundle is read-only)
TEMPLATES_DIR   = _HERE / 'templates'
XBOX_HEADER_TPL = TEMPLATES_DIR / 'xbox360_header.bin'

# STFS metadata layout (Horizon's XContentStructure.cs):
#   0x0411  DisplayNames[9]  x 0x100   -> 0x0D11
#   0x0D11  Descriptions[9]  x 0x100   -> 0x1611
#   0x1611  Publisher 0x80 / 0x1691 TitleName 0x80 / 0x1711 TransferFlags
#   0x1712  ThumbnailSize u32 / 0x1716 TitleThumbnailSize u32
#   0x171A  Thumbnail (0x3D00 for metadata v2, 0x4000 for v1)
DISPLAY_NAME_OFF   = 0x411
DISPLAY_NAME_SIZE  = 0x100
DISPLAY_NAME_SLOTS = 9

META_VERSION_OFF   = 0x348
THUMB_SIZE_OFF     = 0x1712
THUMB_DATA_OFF     = 0x171A

EMU_THUMB_NAME = '__thumbnail.png'
EMU_DAT_NAME   = 'savegame.dat'

# A native Win64 world folder holds these alongside saveData.ms.
WORLDNAME_TXT   = 'worldname.txt'
WIN64_THUMB_REL = Path('thumbnails') / 'thumbData.png'

PS3_TITLE_ID = 'NPEB01899'

# Windows LCE is a single release built on the same feature set as TU19 (CU7),
# so every world in it is TU19 - there is nothing to narrow. The number, not
# the sentence; WINDOWS_LCE_TITLE_UPDATE further down is the wording for the
# info page and would come out of a list column as a paragraph.
WINDOWS_LCE_TU = 19


def sanitise(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    s = re.sub(r'_+', '_', s).strip('_. ')
    return s[:64] or 'MinecraftSave'


# =============================================================================
# XMemCompress / XMemDecompress  (xcompress64.dll)
# =============================================================================

class _XMEMCODEC_PARAMETERS_LZX(ctypes.Structure):
    _fields_ = [('Flags',                    ctypes.c_uint32),
                ('WindowSize',               ctypes.c_uint32),
                ('CompressionPartitionSize', ctypes.c_uint32)]


_XC_DLL = None
_XC_PATH = _HERE / 'xcompress64.dll'


def _get_xcompress():
    global _XC_DLL
    if _XC_DLL is not None:
        return _XC_DLL
    if not _XC_PATH.exists():
        raise FileNotFoundError(
            f"xcompress64.dll not found at {_XC_PATH}\n"
            "It is required to LZX-compress data for the Xbox 360.")
    dll = ctypes.WinDLL(str(_XC_PATH))
    H, S, R = ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int32

    dll.XMemCreateCompressionContext.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(H)]
    dll.XMemCreateCompressionContext.restype = R
    dll.XMemCompress.argtypes = [H, ctypes.c_void_p, ctypes.POINTER(S),
                                 ctypes.c_void_p, S]
    dll.XMemCompress.restype = R
    dll.XMemDestroyCompressionContext.argtypes = [H]
    dll.XMemDestroyCompressionContext.restype = None

    dll.XMemCreateDecompressionContext.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(H)]
    dll.XMemCreateDecompressionContext.restype = R
    dll.XMemDecompress.argtypes = [H, ctypes.c_void_p, ctypes.POINTER(S),
                                   ctypes.c_void_p, S]
    dll.XMemDecompress.restype = R
    dll.XMemDestroyDecompressionContext.argtypes = [H]
    dll.XMemDestroyDecompressionContext.restype = None

    _XC_DLL = dll
    return dll


def xmem_compress(data: bytes) -> bytes:
    """Compress with the exact codec settings the LCE engine uses."""
    dll = _get_xcompress()
    H, S = ctypes.c_void_p, ctypes.c_uint64
    params = _XMEMCODEC_PARAMETERS_LZX(0, LZX_WINDOW_SIZE, LZX_PARTITION_SIZE)
    ctx = H()
    hr = dll.XMemCreateCompressionContext(1, ctypes.byref(params), 0,
                                          ctypes.byref(ctx))
    if hr != 0 or not ctx.value:
        raise RuntimeError(
            f"XMemCreateCompressionContext failed (hr=0x{hr & 0xFFFFFFFF:08X})")
    try:
        cap = max(len(data) * 2 + 0x10000, 0x20000)
        out = ctypes.create_string_buffer(cap)
        osz = S(cap)
        src = ctypes.create_string_buffer(data, len(data))
        hr = dll.XMemCompress(ctx, out, ctypes.byref(osz), src, S(len(data)))
        if hr != 0:
            raise RuntimeError(f"XMemCompress failed (hr=0x{hr & 0xFFFFFFFF:08X})")
        return bytes(out.raw[: osz.value])
    finally:
        dll.XMemDestroyCompressionContext(ctx)


def xmem_decompress(stream: bytes, out_size: int) -> bytes:
    """Decompress with the same decoder the console runs."""
    dll = _get_xcompress()
    H, S = ctypes.c_void_p, ctypes.c_uint64
    params = _XMEMCODEC_PARAMETERS_LZX(0, LZX_WINDOW_SIZE, LZX_PARTITION_SIZE)
    ctx = H()
    hr = dll.XMemCreateDecompressionContext(1, ctypes.byref(params), 0,
                                            ctypes.byref(ctx))
    if hr != 0 or not ctx.value:
        raise RuntimeError(
            f"XMemCreateDecompressionContext failed (hr=0x{hr & 0xFFFFFFFF:08X})")
    try:
        pad = 0x1000                       # decoder can touch past the logical end
        src = ctypes.create_string_buffer(stream + b'\x00' * pad, len(stream) + pad)
        dst = ctypes.create_string_buffer(out_size + pad)
        osz = S(out_size)
        hr = dll.XMemDecompress(ctx, dst, ctypes.byref(osz), src, S(len(stream)))
        if hr != 0:
            raise RuntimeError(f"XMemDecompress failed (hr=0x{hr & 0xFFFFFFFF:08X})")
        return bytes(dst.raw[: osz.value])
    finally:
        dll.XMemDestroyDecompressionContext(ctx)


def _compress_lzx_verified(data: bytes, verify: bool, what: str) -> bytes:
    packed = xmem_compress(data)
    if verify:
        back = xmem_decompress(packed, len(data))
        if back[:len(data)] != data:
            raise RuntimeError(
                f"LZX self-check failed for {what}: {len(data)} bytes in, "
                f"{len(back)} bytes back out")
    return packed


def _compress_ps3_verified(data: bytes, verify: bool, what: str) -> bytes:
    """PS3 chunk payload: [u32 BE rleSize][raw deflate]."""
    co = zlib.compressobj(6, zlib.DEFLATED, -15)
    packed = co.compress(data) + co.flush()
    blob = struct.pack('>I', len(data)) + packed
    if verify:
        back = zlib.decompress(blob[4:], -15)
        if back != data:
            raise RuntimeError(
                f"deflate self-check failed for {what}: {len(data)} bytes in, "
                f"{len(back)} bytes back out")
    return blob


# =============================================================================
# Payload parsing / building
# =============================================================================

def parse_payload(data: bytes, endian: str):
    """Parse the 12-byte 4J header and file table. *endian* is '<' or '>'."""
    fto, ne = struct.unpack_from(endian + 'II', data, 0)
    ov, cv = struct.unpack_from(endian + 'hh', data, 8)

    if fto < HEADER_SIZE or fto > len(data):
        raise RuntimeError(f"Bad file-table offset {fto} (payload {len(data)} bytes)")
    if len(data) - fto != ne * FILE_ENTRY_SIZE:
        raise RuntimeError(
            f"File table size mismatch: {len(data) - fto} bytes present, "
            f"{ne} entries x {FILE_ENTRY_SIZE} = {ne * FILE_ENTRY_SIZE} expected")

    codec = 'utf-16-le' if endian == '<' else 'utf-16-be'
    entries = []
    for i in range(ne):
        base = fto + i * FILE_ENTRY_SIZE
        raw = data[base: base + FILE_ENTRY_SIZE]
        entries.append({
            'filename':     raw[:128].decode(codec).split('\x00')[0],
            'length':       struct.unpack_from(endian + 'I', raw, 128)[0],
            'start_offset': struct.unpack_from(endian + 'I', raw, 132)[0],
            'last_mod':     struct.unpack_from(endian + 'q', raw, 136)[0],
        })
    return fto, ne, ov, cv, entries


def build_payload(ov: int, cv: int, entries: list, blobs: list,
                  endian: str = '>') -> bytes:
    """Assemble a 4J payload: header + file bodies + file table."""
    codec = 'utf-16-le' if endian == '<' else 'utf-16-be'
    body = bytearray()
    cursor = HEADER_SIZE
    placed = []

    for entry, blob in zip(entries, blobs):
        placed.append((entry['filename'], len(blob), cursor, entry['last_mod']))
        body.extend(blob)
        cursor += len(blob)

    ftable = bytearray()
    for filename, length, start, last_mod in placed:
        ftable.extend(filename.encode(codec)[:128].ljust(128, b'\x00'))
        ftable.extend(struct.pack(endian + 'IIq', length, start, last_mod))

    header = struct.pack(endian + 'IIhh', cursor, len(placed), ov, cv)
    return bytes(header) + bytes(body) + bytes(ftable)


# =============================================================================
# Region conversion: Win64 (LE, zlib) -> console (BE, LZX or deflate)
# =============================================================================

def convert_region_to_console(data: bytes, platform: str, log=None,
                              verify: bool = True, name: str = '?',
                              region=None, bounds=None, dropped=None) -> bytes:
    """
    Rebuild a .mcr region file for the target console.

    Reads the little-endian tables and each chunk's zlib payload, recompresses
    the untouched RLE bytes for the target, and writes everything back
    big-endian, repacking chunks from sector 2 upward.
    """
    if len(data) < SECT * 2:
        return data

    def note(msg):
        if log:
            log(msg)

    def in_console_world(slot):
        """False for chunks that lie outside the console's smaller world."""
        if region is None or bounds is None:
            return True
        lo, hi = bounds
        cx = region[0] * CHUNKS_PER_REGION + (slot % CHUNKS_PER_REGION)
        cz = region[1] * CHUNKS_PER_REGION + (slot // CHUNKS_PER_REGION)
        return lo <= cx <= hi and lo <= cz <= hi

    chunks = {}
    cropped = 0
    for slot in range(REGION_SECT_COUNT):
        v = struct.unpack_from('<I', data, slot * 4)[0]
        if v == 0:
            continue
        sn = (v >> 8) & 0xFFFFFF
        if sn < 2:
            continue
        fo = sn * SECT
        if fo + 8 > len(data):
            continue
        if not in_console_world(slot):
            cropped += 1
            continue
        chunks[slot] = fo

    if cropped and dropped is not None:
        dropped[0] = dropped[0] + cropped

    new_buf = bytearray(SECT * 2)
    for slot in range(REGION_SECT_COUNT):          # timestamps: LE -> BE
        ts = struct.unpack_from('<I', data, SECT + slot * 4)[0]
        struct.pack_into('>I', new_buf, SECT + slot * 4, ts)

    next_sector = 2
    kept = dropped = 0

    for slot in sorted(chunks, key=lambda s: chunks[s]):
        fo = chunks[slot]
        comp_raw, decomp_len = struct.unpack_from('<II', data, fo)
        use_rle = bool(comp_raw & CHUNK_FLAG_RLE)
        comp_len = comp_raw & CHUNK_LEN_MASK

        if comp_len == 0 or fo + 8 + comp_len > len(data):
            note(f"chunk slot {slot}: truncated (compLen={comp_len}), dropped")
            dropped += 1
            continue

        try:
            rle_data = zlib.decompress(data[fo + 8: fo + 8 + comp_len])
        except zlib.error as exc:
            note(f"chunk slot {slot}: zlib decompress failed ({exc}), dropped")
            dropped += 1
            continue

        try:
            if platform == 'ps3':
                payload = _compress_ps3_verified(rle_data, verify,
                                                 f"{name} slot {slot}")
            else:
                payload = _compress_lzx_verified(rle_data, verify,
                                                 f"{name} slot {slot}")
        except Exception as exc:
            note(f"chunk slot {slot}: compression failed ({exc}), dropped")
            dropped += 1
            continue

        needed = (8 + len(payload) + SECT - 1) // SECT
        if needed > 0xFF:
            note(f"chunk slot {slot}: {needed} sectors exceeds the 255 the "
                 f"location table can address, dropped")
            dropped += 1
            continue

        dest_off = next_sector * SECT
        end = dest_off + needed * SECT
        if end > len(new_buf):
            new_buf.extend(b'\x00' * (end - len(new_buf)))

        struct.pack_into('>I', new_buf, dest_off,
                         len(payload) | (CHUNK_FLAG_RLE if use_rle else 0))
        struct.pack_into('>I', new_buf, dest_off + 4, decomp_len)
        new_buf[dest_off + 8: dest_off + 8 + len(payload)] = payload
        struct.pack_into('>I', new_buf, slot * 4, (next_sector << 8) | needed)

        next_sector += needed
        kept += 1

    if dropped:
        note(f"{kept} chunks converted, {dropped} dropped")
    return bytes(new_buf)


# =============================================================================
# savegame.dat container
# =============================================================================

SAVEGAME_ALIGN = 512


def build_savegame_dat(be_payload: bytes, verify: bool = True) -> bytes:
    """Wrap a big-endian payload in the Xbox 360 savegame.dat container.

    The whole file is padded out to a 512-byte boundary with zeros. Every
    genuine game-written savegame.dat is 512-aligned; the emulator reads the
    file in 512-byte sectors, and a short, unpadded file freezes the world at
    load. read_savegame_dat ignores the trailing padding - it is sized from the
    header's length word - so the pad is write-only, matching what the game does.
    """
    lzx = _compress_lzx_verified(be_payload, verify, "savegame.dat")
    dat = struct.pack('>III', 8 + len(lzx), 0, len(be_payload)) + lzx
    pad = (-len(dat)) % SAVEGAME_ALIGN
    return dat + b'\x00' * pad


def read_savegame_dat(dat: bytes) -> bytes:
    """Unwrap savegame.dat -> the big-endian payload."""
    if len(dat) < 12:
        raise RuntimeError("savegame.dat is too small to be valid.")
    payload_len, zero, uncomp = struct.unpack_from('>III', dat, 0)
    if zero != 0:
        raise RuntimeError(
            f"savegame.dat header field at 0x04 is {zero}, expected 0.")
    # Decode everything after the 12-byte header and let the uncompressed size be the
    # stop condition (the decoder ignores trailing pad). This is robust to the exact
    # length-word convention: a genuine save's length word points just past the stream,
    # while a save written by LCEStudio's own compressor carries a slightly larger word
    # and a longer stream -- slicing by the word would truncate it and crash the decoder.
    return xmem_decompress(dat[12:], uncomp)


# =============================================================================
# STFS CON package
# =============================================================================

def stfs_backing_data_block(block: int, shift: int = STFS_FORMAT_SHIFT) -> int:
    """
    Backing block number of a data block.
    Port of Horizon's StfsComputeBackingDataBlockNumber.
    """
    l0, l1 = STFS_BLOCKS_PER_GRP, STFS_BLOCKS_PER_L1
    num = (((block + l0) // l0) << shift) + block
    if block < l0:
        return num
    num = (((block + l1) // l1) << shift) + num
    if block < l1:
        return num
    return num + (1 << shift)


def stfs_backing_hash_block(block: int, level: int,
                            shift: int = STFS_FORMAT_SHIFT,
                            values: tuple = STFS_BLOCK_VALUES) -> int:
    """
    Backing block number of the level-N hash table covering *block*.
    Port of Horizon's StfsComputeLevelNBackingHashBlockNumber.
    """
    l0, l1 = STFS_BLOCKS_PER_GRP, STFS_BLOCKS_PER_L1
    if level == 0:
        n = block // l0
        num = n * values[0]
        if n == 0:
            return num
        n = block // l1
        num += (n + 1) << shift
        if n == 0:
            return num
        return num + (1 << shift)
    if level == 1:
        n = block // l1
        num = n * values[1]
        if n == 0:
            return num + values[0]
        return num + (1 << shift)
    if level == 2:
        return values[1]
    raise ValueError(f"bad hash tree level {level}")


def stfs_root_hierarchy(total_blocks: int) -> int:
    """Which hash level the volume descriptor's RootHash covers."""
    if total_blocks > STFS_BLOCKS_PER_L1:
        return 2
    if total_blocks > STFS_BLOCKS_PER_GRP:
        return 1
    return 0


def _block_off(backing_block: int) -> int:
    return STFS_BASE_OFFSET + backing_block * STFS_BLOCK_SIZE


def _stfs_block_offset(block: int) -> int:
    """File offset of a data block."""
    return _block_off(stfs_backing_data_block(block))


def load_stfs_header(template_raw: bytes = None) -> bytes:
    """The 0xA000-byte STFS metadata block to build a package around."""
    if template_raw is not None:
        if template_raw[:4] != STFS_MAGIC:
            raise ValueError(
                f"Template is not an STFS CON file (magic={template_raw[:4]!r})")
        if len(template_raw) < STFS_BASE_OFFSET:
            raise ValueError("Template is truncated - it has no STFS header.")
        return template_raw[:STFS_BASE_OFFSET]

    if not XBOX_HEADER_TPL.exists():
        raise FileNotFoundError(
            f"No template given and {XBOX_HEADER_TPL} is missing.\n"
            "Supply an Xbox 360 .bin as a template, or restore the file "
            "(the first 40960 bytes of any Xbox 360 LCE save).")
    header = XBOX_HEADER_TPL.read_bytes()
    if header[:4] != STFS_MAGIC or len(header) < STFS_BASE_OFFSET:
        raise ValueError(f"{XBOX_HEADER_TPL.name} is not a valid STFS header.")
    return header[:STFS_BASE_OFFSET]


def read_stfs_thumbnail(raw: bytes):
    """
    The package's thumbnail, using the ThumbnailSize field as the authority.

    converter.py's STFSPackage.thumbnail scans for the PNG's IEND marker and
    takes iend + 12, but a PNG ends 8 bytes after that marker (4-byte type +
    4-byte CRC), so it returns 4 bytes of zero padding as well. Left alone,
    every console -> Windows LCE conversion grows the thumbnail by 4 bytes.
    """
    if len(raw) < THUMB_DATA_OFF + 4:
        return None
    size = struct.unpack_from('>I', raw, THUMB_SIZE_OFF)[0]
    meta_version = struct.unpack_from('>I', raw, META_VERSION_OFF)[0]
    max_size = 0x3D00 if meta_version >= 2 else 0x4000
    if not 0 < size <= max_size:
        return None
    png = raw[THUMB_DATA_OFF: THUMB_DATA_OFF + size]
    return png if png[:8] == PNG_MAGIC else None


def _set_stfs_thumbnail(out: bytearray, png: bytes) -> bool:
    """Write the world's thumbnail into the STFS metadata. True if it fit."""
    if not png or png[:8] != PNG_MAGIC:
        return False
    meta_version = struct.unpack_from('>I', out, META_VERSION_OFF)[0]
    max_size = 0x3D00 if meta_version >= 2 else 0x4000
    if len(png) > max_size:
        return False
    struct.pack_into('>I', out, THUMB_SIZE_OFF, len(png))
    out[THUMB_DATA_OFF: THUMB_DATA_OFF + max_size] = png.ljust(max_size, b'\x00')
    return True


def build_stfs_con(savegame_dat: bytes, template_raw: bytes = None,
                   display_name: str = None, thumbnail: bytes = None,
                   profile_id=None) -> bytes:
    """
    Build an STFS CON package holding savegame.dat.

    The metadata block (title/profile/console/device IDs, thumbnail) comes from
    the template or the bundled default; the volume descriptor, file table,
    data blocks and hash tables are rebuilt, and the package is console-signed
    on the way out - the same rehash and resign Horizon performs, so it does
    not need a trip through Horizon afterwards.
    """
    header = load_stfs_header(template_raw)

    dat_blocks = max(1, (len(savegame_dat) + STFS_BLOCK_SIZE - 1) // STFS_BLOCK_SIZE)
    total_blocks = 1 + dat_blocks
    num_groups = (total_blocks + STFS_BLOCKS_PER_GRP - 1) // STFS_BLOCKS_PER_GRP

    out = bytearray(_stfs_block_offset(total_blocks - 1) + STFS_BLOCK_SIZE)
    out[:STFS_BASE_OFFSET] = header

    if display_name:
        enc = display_name.encode('utf-16-be')[:DISPLAY_NAME_SIZE - 2]
        enc = enc.ljust(DISPLAY_NAME_SIZE, b'\x00')
        for slot in range(DISPLAY_NAME_SLOTS):
            off = DISPLAY_NAME_OFF + slot * DISPLAY_NAME_SIZE
            out[off: off + DISPLAY_NAME_SIZE] = enc

    if thumbnail:
        _set_stfs_thumbnail(out, thumbnail)

    if profile_id:
        out[PROFILE_ID_OFF: PROFILE_ID_OFF + PROFILE_ID_SIZE] = \
            parse_profile_id(profile_id)

    out[VD_LENGTH_OFF] = 0x24
    out[VD_VERSION_OFF] = 0
    # Flags must be 0: ReadOnlyFormat=0 selects the FormatShift=1 geometry we
    # actually lay the file out with, and RootActiveIndex=0 points the root
    # hash at the primary table.
    out[VD_FLAGS_OFF] = 0
    struct.pack_into('<H', out, VD_DIRALLOC_OFF, 1)
    out[VD_DIRBLOCK_OFF] = out[VD_DIRBLOCK_OFF + 1] = out[VD_DIRBLOCK_OFF + 2] = 0
    struct.pack_into('>I', out, VD_TOTAL_OFF, total_blocks)
    struct.pack_into('>I', out, VD_FREE_OFF, 0)
    struct.pack_into('>I', out, 0x39D, 1)
    struct.pack_into('>q', out, 0x3A1, len(savegame_dat))
    struct.pack_into('>q', out, 0x34C, len(savegame_dat))

    entry = bytearray(64)
    name = b'savegame.dat'
    entry[:len(name)] = name
    entry[0x28] = len(name) | 0x40
    for off in (0x29, 0x2C):
        entry[off] = dat_blocks & 0xFF
        entry[off + 1] = (dat_blocks >> 8) & 0xFF
        entry[off + 2] = (dat_blocks >> 16) & 0xFF
    entry[0x2F] = 1
    struct.pack_into('>h', entry, 0x32, -1)
    struct.pack_into('>I', entry, 0x34, len(savegame_dat))
    ft_off = _stfs_block_offset(0)
    out[ft_off: ft_off + 64] = entry

    for i in range(dat_blocks):
        off = _stfs_block_offset(1 + i)
        chunk = savegame_dat[i * STFS_BLOCK_SIZE: (i + 1) * STFS_BLOCK_SIZE]
        out[off: off + len(chunk)] = chunk

    # ---- level 0 hash tables: one per group of 170 data blocks -------------
    l0_hashes = []
    for g in range(num_groups):
        first = g * STFS_BLOCKS_PER_GRP
        last = min(first + STFS_BLOCKS_PER_GRP, total_blocks)
        table = bytearray(STFS_BLOCK_SIZE)
        for i in range(last - first):
            block = first + i
            off = _stfs_block_offset(block)
            e = i * 0x18
            table[e: e + 20] = hashlib.sha1(
                bytes(out[off: off + STFS_BLOCK_SIZE])).digest()
            # entry+0x14 is a big-endian dword; bit 31 marks the block in use
            # and bit 30 is the active index, which must stay 0 so readers take
            # the primary table.
            table[e + 20] = 0x80
            nxt = block + 1 if 1 <= block < total_blocks - 1 else 0xFFFFFF
            table[e + 21] = (nxt >> 16) & 0xFF
            table[e + 22] = (nxt >> 8) & 0xFF
            table[e + 23] = nxt & 0xFF

        primary = _block_off(stfs_backing_hash_block(first, 0))
        out[primary: primary + STFS_BLOCK_SIZE] = table
        out[primary + STFS_BLOCK_SIZE: primary + 2 * STFS_BLOCK_SIZE] = table
        l0_hashes.append(hashlib.sha1(bytes(table)).digest())

    # ---- level 1 table, when the root hierarchy needs one -----------------
    if stfs_root_hierarchy(total_blocks) >= 1:
        l1 = bytearray(STFS_BLOCK_SIZE)
        for i, h in enumerate(l0_hashes):
            e = i * 0x18
            l1[e: e + 20] = h
            l1[e + 20] = 0x80
        primary = _block_off(stfs_backing_hash_block(0, 1))
        out[primary: primary + STFS_BLOCK_SIZE] = l1
        out[primary + STFS_BLOCK_SIZE: primary + 2 * STFS_BLOCK_SIZE] = l1
        top_hash = hashlib.sha1(bytes(l1)).digest()
    else:
        top_hash = l0_hashes[0]

    out[VD_ROOTHASH_OFF: VD_ROOTHASH_OFF + 20] = top_hash

    # ContentId at 0x32C is the hash over the whole metadata region, so it has
    # to be computed last - after the display name, thumbnail, volume
    # descriptor and root hash are all in place.
    out[CONTENT_ID_OFF: CONTENT_ID_OFF + 20] = hashlib.sha1(
        bytes(out[METADATA_OFF: STFS_BASE_OFFSET])).digest()

    # Signing goes last of all: the ContentId just written is inside the block
    # the signature covers.
    return resign_con(bytes(out))


def _con_private_exponent() -> int:
    """
    Recover d from e, p and q.

    Horizon stores con_d as 128 zero bytes and lets .NET rebuild it from the
    CRT parameters. d = e^-1 mod lcm(p-1, q-1) gives the same number, which
    _con_key_ok() confirms against the stored dp, dq and inverse q.
    """
    p = int.from_bytes(lce_conkey.CON_P, 'big')
    q = int.from_bytes(lce_conkey.CON_Q, 'big')
    e = lce_conkey.CON_EXPONENT
    lam = (p - 1) * (q - 1) // math.gcd(p - 1, q - 1)
    return pow(e, -1, lam)


def _con_key_ok() -> bool:
    """Check the embedded key is internally consistent before trusting it."""
    p = int.from_bytes(lce_conkey.CON_P, 'big')
    q = int.from_bytes(lce_conkey.CON_Q, 'big')
    n = int.from_bytes(lce_conkey.CON_MODULUS, 'big')
    d = _con_private_exponent()
    return (p * q == n
            and d % (p - 1) == int.from_bytes(lce_conkey.CON_DP, 'big')
            and d % (q - 1) == int.from_bytes(lce_conkey.CON_DQ, 'big')
            and (int.from_bytes(lce_conkey.CON_INVERSE_Q, 'big') * q) % p == 1)


def _con_sign(data: bytes) -> bytes:
    """
    RSA-SHA1 sign, PKCS#1 v1.5, then reverse the bytes.

    The reverse is not decoration - HorizonCrypt.FormatSignature does
    Array.Reverse on what .NET produces, because the 360 reads the signature
    little-endian while .NET writes it big-endian.
    """
    n = int.from_bytes(lce_conkey.CON_MODULUS, 'big')
    k = len(lce_conkey.CON_MODULUS)
    t = _SHA1_DIGESTINFO + hashlib.sha1(data).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    sig = pow(int.from_bytes(em, 'big'), _con_private_exponent(), n)
    return sig.to_bytes(k, 'big')[::-1]


def resign_con(raw: bytes) -> bytes:
    """
    Console-sign an STFS package, the way Horizon's "Rehash & Resign" does.

    Replaces the certificate with the one the key belongs to and signs the
    0x118-byte metadata block. The package must already be rehashed, since the
    ContentId at 0x32C sits inside the region being signed.
    """
    if not _con_key_ok():
        raise ValueError("embedded console key failed its self-check")
    out = bytearray(raw)
    out[0:4] = CON_MAGIC
    out[CON_CERT_OFF: CON_CERT_OFF + CON_CERT_SIZE] = lce_conkey.CON_PUBLIC_KEY
    signed = bytes(out[CON_SIGNED_OFF: CON_SIGNED_OFF + CON_SIGNED_SIZE])
    out[CON_SIG_OFF: CON_SIG_OFF + CON_SIG_SIZE] = _con_sign(signed)
    return bytes(out)


def read_profile_id(raw: bytes) -> str:
    """The gamertag id a package is filed under, as 16 hex digits."""
    return raw[PROFILE_ID_OFF: PROFILE_ID_OFF + PROFILE_ID_SIZE].hex().upper()


def parse_profile_id(text) -> bytes:
    """
    Accept a profile id however it was copied - '0xE000...', spaces, lower
    case, or the folder name straight out of Content\\.
    """
    if isinstance(text, (bytes, bytearray)):
        raw = bytes(text)
        if len(raw) != PROFILE_ID_SIZE:
            raise ValueError(f"profile id must be {PROFILE_ID_SIZE} bytes")
        return raw
    # Drop the 0x first: stripping non-hex characters would eat the x and
    # leave the 0 behind, turning a valid id into a 17-digit one.
    s = str(text).strip()
    if s[:2].lower() == '0x':
        s = s[2:]
    s = re.sub(r'[^0-9A-Fa-f]', '', s)
    if len(s) != PROFILE_ID_SIZE * 2:
        raise ValueError(f"profile id must be {PROFILE_ID_SIZE * 2} hex digits, "
                         f"got {len(s)}")
    return bytes.fromhex(s)


def profile_id_from_path(path) -> str:
    """
    Pull the profile id out of a console storage path.

    A memory unit or hard drive lays saves out as

        Content\\<ProfileId>\\<TitleId>\\<SaveId>\\<file>

    so writing into one of those folders already tells us whose save it is -
    no need to ask for something the path is holding.
    """
    for part in reversed(Path(path).parts):
        if len(part) == PROFILE_ID_SIZE * 2:
            try:
                int(part, 16)
            except ValueError:
                continue
            return part.upper()
    return None


def suggest_profile_id(dest=None, src_path=None) -> str:
    """
    The best guess at whose save this should be, most specific first.

    A Content\\<ProfileId>\\ destination is definitive. Failing that, a source
    that is already an Xbox 360 package knows its own owner - round-tripping
    your own world should not hand it to somebody else. Otherwise the template
    decides.
    """
    if dest:
        found = profile_id_from_path(dest)
        if found:
            return found
    if src_path:
        try:
            head = Path(src_path).open('rb').read(STFS_BASE_OFFSET)
            if head[:4] == STFS_MAGIC and len(head) >= STFS_BASE_OFFSET:
                return read_profile_id(head)
        except OSError:
            pass
    try:
        return read_profile_id(load_stfs_header())
    except Exception:
        return DEFAULT_PROFILE_ID


def set_profile_id(raw: bytes, profile_id) -> bytes:
    """
    Refile a package under a different gamertag, then rehash and re-sign.

    ProfileId sits at 0x371, inside the region the ContentId covers, so it
    cannot be edited in place - the ContentId and the signature both have to
    be redone after it.
    """
    out = bytearray(raw)
    out[PROFILE_ID_OFF: PROFILE_ID_OFF + PROFILE_ID_SIZE] = parse_profile_id(profile_id)
    out[CONTENT_ID_OFF: CONTENT_ID_OFF + 20] = hashlib.sha1(
        bytes(out[METADATA_OFF: STFS_BASE_OFFSET])).digest()
    return resign_con(bytes(out))


def check_con_signature(raw: bytes) -> list:
    """
    Verify the console signature against the certificate in the package.

    Returns a list of problems; empty means the signature matches the metadata
    block. Only meaningful for packages we signed - a retail save carries its
    own console's certificate, whose private half nobody has.
    """
    problems = []
    if raw[0:4] != CON_MAGIC:
        problems.append(f"magic is {raw[0:4]!r}, expected {CON_MAGIC!r}")
    cert = raw[CON_CERT_OFF: CON_CERT_OFF + CON_CERT_SIZE]
    if cert != lce_conkey.CON_PUBLIC_KEY:
        problems.append("certificate is not the one we sign with")
        return problems
    n = int.from_bytes(lce_conkey.CON_MODULUS, 'big')
    sig = raw[CON_SIG_OFF: CON_SIG_OFF + CON_SIG_SIZE][::-1]
    em = pow(int.from_bytes(sig, 'big'),
             lce_conkey.CON_EXPONENT, n).to_bytes(len(lce_conkey.CON_MODULUS), 'big')
    want = _SHA1_DIGESTINFO + hashlib.sha1(
        raw[CON_SIGNED_OFF: CON_SIGNED_OFF + CON_SIGNED_SIZE]).digest()
    if not em.endswith(want):
        problems.append("signature does not match the metadata block at 0x22C")
    return problems


def validate_stfs(raw: bytes) -> list:
    """
    Walk an STFS package the way Horizon does and verify every hash.

    Returns a list of problem strings; empty means the package passes the same
    checks Horizon's StfsMapNewBlock performs, which is what produces
    "hash mismatch for block number 0x........:N" when it fails.

    Reads the geometry from the volume descriptor rather than assuming it, so a
    descriptor that disagrees with the layout is caught rather than hidden.
    """
    problems = []
    if raw[:4] != STFS_MAGIC:
        return [f"not an STFS CON package (magic={raw[:4]!r})"]

    flags = raw[VD_FLAGS_OFF]
    read_only = flags & 1
    root_active = (flags >> 1) & 1
    shift = 0 if read_only else 1
    values = (0xAB, 0x718F) if read_only else (0xAC, 0x723A)

    total_blocks = struct.unpack_from('>I', raw, VD_TOTAL_OFF)[0]
    free_blocks = struct.unpack_from('>I', raw, VD_FREE_OFF)[0]
    allocated = total_blocks - free_blocks
    root_hash = raw[VD_ROOTHASH_OFF: VD_ROOTHASH_OFF + 20]
    hierarchy = stfs_root_hierarchy(total_blocks)

    def block(n):
        off = _block_off(n)
        chunk = raw[off: off + STFS_BLOCK_SIZE]
        if len(chunk) < STFS_BLOCK_SIZE:
            return None
        return chunk

    def sha(b):
        return hashlib.sha1(b).digest()

    if hierarchy >= 2:
        return ["packages needing a level-2 hash tree are not supported "
                f"({total_blocks} blocks)"]

    # ---- root hash -> top table -------------------------------------------
    if hierarchy == 1:
        top_bn = stfs_backing_hash_block(0, 1, shift, values) + root_active
        level_name = "L1"
    else:
        top_bn = stfs_backing_hash_block(0, 0, shift, values) + root_active
        level_name = "L0"

    top = block(top_bn)
    if top is None:
        return [f"{level_name} table at backing block {top_bn} is past EOF"]
    if sha(top) != root_hash:
        problems.append(
            f"root hash does not match the {level_name} table at backing "
            f"block {top_bn} (this is Horizon's "
            f"'hash mismatch ... :{hierarchy + 1}')")

    # ---- L1 entries -> L0 tables ------------------------------------------
    num_groups = (allocated + STFS_BLOCKS_PER_GRP - 1) // STFS_BLOCKS_PER_GRP
    if hierarchy == 1:
        for g in range(num_groups):
            e = g * 0x18
            expect = top[e: e + 20]
            l0_bn = stfs_backing_hash_block(g * STFS_BLOCKS_PER_GRP, 0,
                                            shift, values)
            active = (top[e + 20] >> 6) & 1
            t = block(l0_bn + active)
            if t is None:
                problems.append(f"L0 table for group {g} is past EOF")
                continue
            if sha(t) != expect:
                problems.append(
                    f"L1 entry {g} does not match its L0 table at backing "
                    f"block {l0_bn + active}")

    # ---- L0 entries -> data blocks ----------------------------------------
    bad_blocks = 0
    for b in range(allocated):
        g, i = divmod(b, STFS_BLOCKS_PER_GRP)
        l0_bn = stfs_backing_hash_block(g * STFS_BLOCKS_PER_GRP, 0, shift, values)
        t = block(l0_bn)
        if t is None:
            problems.append(f"L0 table for group {g} is past EOF")
            break
        e = i * 0x18
        data = block(stfs_backing_data_block(b, shift))
        if data is None:
            problems.append(f"data block {b} is past EOF")
            break
        if sha(data) != t[e: e + 20]:
            bad_blocks += 1
            if bad_blocks <= 3:
                problems.append(
                    f"hash mismatch for data block {b} "
                    f"(L0 group {g} entry {i})")
    if bad_blocks > 3:
        problems.append(f"... and {bad_blocks - 3} more data block mismatches")

    # ---- ContentId over the metadata region --------------------------------
    if raw[CONTENT_ID_OFF: CONTENT_ID_OFF + 20] != sha(
            raw[METADATA_OFF: STFS_BASE_OFFSET]):
        problems.append(
            f"ContentId at 0x{CONTENT_ID_OFF:X} does not match SHA1 of the "
            f"metadata region 0x{METADATA_OFF:X}..0x{STFS_BASE_OFFSET:X}")

    return problems


# =============================================================================
# PARAM.SFO
# =============================================================================

SFO_FMT_UTF8_STR = 0x0204
SFO_FMT_INT32    = 0x0404


def build_param_sfo(world_name: str, dir_name: str) -> bytes:
    """
    Build a minimal PARAM.SFO for a PS3 Minecraft save.

    The world name lives in SUB_TITLE - that is where converter_ps3.py reads it
    from, because level.dat's LevelName is always the literal "world" on PS3.
    """
    fields = [
        ('ACCOUNT_ID',          SFO_FMT_UTF8_STR, b'0000000000000000\x00', 17),
        ('ATTRIBUTE',           SFO_FMT_INT32,    struct.pack('<I', 0), 4),
        ('CATEGORY',            SFO_FMT_UTF8_STR, b'SD\x00', 4),
        ('DETAIL',              SFO_FMT_UTF8_STR, b'\x00', 1024),
        ('PARAMS',              SFO_FMT_UTF8_STR, b'\x00' * 1024, 1024),
        ('SAVEDATA_DIRECTORY',  SFO_FMT_UTF8_STR,
         dir_name.encode('utf-8')[:63] + b'\x00', 64),
        ('SAVEDATA_LIST_PARAM', SFO_FMT_UTF8_STR, b'\x00', 8),
        ('SUB_TITLE',           SFO_FMT_UTF8_STR,
         world_name.encode('utf-8')[:127] + b'\x00', 128),
        ('TITLE',               SFO_FMT_UTF8_STR, 'Minecraft'.encode('utf-8') + b'\x00', 128),
    ]

    n = len(fields)
    key_table = bytearray()
    key_offsets = []
    for key, _, _, _ in fields:
        key_offsets.append(len(key_table))
        key_table.extend(key.encode('ascii') + b'\x00')
    while len(key_table) % 4:
        key_table.append(0)

    data_table = bytearray()
    data_offsets = []
    for _, _, value, maxlen in fields:
        data_offsets.append(len(data_table))
        data_table.extend(value.ljust(maxlen, b'\x00')[:maxlen])

    key_start = 0x14 + n * 16
    data_start = key_start + len(key_table)

    out = bytearray()
    out += b'\x00PSF' + struct.pack('<I', 0x00000101)
    out += struct.pack('<III', key_start, data_start, n)
    for i, (key, fmt, value, maxlen) in enumerate(fields):
        out += struct.pack('<HHIII', key_offsets[i], fmt, len(value),
                           maxlen, data_offsets[i])
    out += key_table
    out += data_table
    return bytes(out)


# =============================================================================
# Win64 saveData.ms
# =============================================================================

def read_savedata_ms(path) -> bytes:
    """Decompress saveData.ms -> the little-endian payload."""
    data = Path(path).read_bytes()
    if len(data) < 8:
        raise RuntimeError("saveData.ms is too small to be valid.")
    flag, uncomp = struct.unpack_from('<II', data, 0)
    if flag != 0:
        raise RuntimeError(
            f"saveData.ms header field 0 is {flag}, expected 0. "
            "This does not look like a Win64 LCE save.")
    payload = zlib.decompress(data[8:])
    if len(payload) != uncomp:
        raise RuntimeError(
            f"saveData.ms declares {uncomp:,} bytes but decompressed to "
            f"{len(payload):,}.")
    return payload


def write_savedata_ms(le_payload: bytes) -> bytes:
    return (struct.pack('<II', 0, len(le_payload))
            + zlib.compress(le_payload, 6))


# =============================================================================
# Input discovery
# =============================================================================

def find_win64_save(path) -> Path:
    p = Path(path)
    if p.is_dir():
        p = p / 'saveData.ms'
    if not p.exists():
        raise FileNotFoundError(f"{p} not found")
    return p


SAVE_FILE_NAMES = ('savedata.ms', 'savegame.dat', 'gamedata')


# Where a thumbnail lives, by platform: emulator folder, Win64 world, PS3 save.
THUMBNAIL_CANDIDATES = (
    Path(EMU_THUMB_NAME),
    Path('thumbnail.png'),
    WIN64_THUMB_REL,
    Path('THUMB'),
)


def find_thumbnail(folder, prefer=None):
    """
    The world's thumbnail from its folder, whatever the platform called it.

    *prefer* puts one filename first. A world folder can end up holding more
    than one - convert a Windows LCE world to emulator format in place and it
    gains a __thumbnail.png alongside its own thumbnails/thumbData.png - so the
    platform's own file has to win, or the wrong image is shown.
    """
    order = list(THUMBNAIL_CANDIDATES)
    if prefer is not None:
        prefer = Path(prefer)
        order = [prefer] + [c for c in order if c != prefer]
    for rel in order:
        p = Path(folder) / rel
        if p.exists() and p.is_file():
            data = p.read_bytes()
            if data[:8] == PNG_MAGIC:
                return data
    return None


def read_worldname_txt(folder):
    """The world's real name from worldname.txt, or None if there isn't one."""
    f = Path(folder) / WORLDNAME_TXT
    if not f.exists():
        return None
    try:
        name = f.read_bytes().decode('utf-8-sig').strip()
    except (UnicodeDecodeError, OSError):
        return None
    return name or None


def world_name_for(path) -> str:
    """
    The world's name, from the same places the world list reads it.

    The GAME's records first - _MinecraftSaveInfo, then the .header - and
    only then worldname.txt and the folder. That order matters here as much
    as in the scan: an emulator world's folder is a timestamp, so falling
    back to it names the world "Save20260807164250.bin", and converting then
    writes that into the new world's index and header as if it were real.
    """
    p = Path(path)
    folder = None
    if p.is_dir():
        folder = p
    elif p.name.lower() in SAVE_FILE_NAMES:
        folder = p.parent

    if folder is None:
        return sanitise(p.stem)

    for entry in read_saveinfo(folder.parent):
        if entry['folder'].lower() == folder.name.lower() and entry['name']:
            return sanitise(entry['name'])
    from_header = read_save_header_name(folder.parent, folder.name)
    if from_header:
        return sanitise(from_header)
    # The history holds the name for a Windows LCE world, which has no index
    # and no header. Read before worldname.txt, which is only there on worlds
    # this tool wrote before the side file was dropped.
    from_history = read_history(folder)[0].get('World name')
    if from_history:
        return sanitise(from_history)
    named = read_worldname_txt(folder)
    if named:
        return sanitise(named)
    return sanitise(folder.name)


# Where the Windows LCE build keeps its worlds, for the folder prompt.
WORLDS_FOLDER_HINT = r"usually Windows LCE\Windows64\GameHDD"

# Each place worlds can live. Point the setting at the game or emulator folder
# and the matching subpath is searched, so nobody has to type a deep path.
#
#   Windows LCE   <game>\Windows64\GameHDD\<world>\saveData.ms
#   Xenia         <xenia>\content\<profile>\<title>\...\savegame.dat
#   Nexia360      <nexia>\Library\584111F7\Title Update 14\Content\
#                     B13EBABEBABEBABE\00000001\savegame.dat
#
# Emulator folders are numbered, not named, and a bare savegame.dat records no
# world name (level.dat's LevelName is the literal "world" on every console
# save), so those worlds are identified by title update and thumbnail instead.
WORLD_SOURCES = {
    'windows_lce': {
        'label': 'Windows LCE',
        'platform': 'windows_lce',
        'subpaths': ['Windows64/GameHDD', 'GameHDD', ''],
        'target': 'saveData.ms',
        'recursive': False,
        'detail': 'Windows LCE',
        'hint': r'the game folder, the one holding Windows64\GameHDD',
    },
    'nexia360': {
        'label': 'Nexia360',
        'platform': 'xbox360',
        'subpaths': ['Library', ''],
        'target': 'savegame.dat',
        'recursive': True,
        'detail': 'Nexia360',
        'hint': r'the Nexia360 folder, the one holding Library',
    },
    'xenia': {
        'label': 'Xenia',
        'platform': 'xbox360',
        'subpaths': ['content', ''],
        'target': 'savegame.dat',
        'recursive': True,
        'detail': 'Xenia',
        'hint': r'the Xenia folder, the one holding content',
    },
    'ps3': {
        'label': 'PS3',
        'platform': 'ps3',
        'subpaths': [''],
        'target': 'GAMEDATA',
        'recursive': True,
        'detail': 'PS3',
        'hint': 'the folder holding your PS3 save folders',
    },
    'java': {
        'label': 'Java',
        'platform': 'java',
        'subpaths': ['saves', ''],
        'target': 'level.dat',
        'recursive': False,
        'detail': 'Java',
        'hint': r'your .minecraft folder, the one holding saves',
    },
}

_TU_TEXT_RE = re.compile(r'(?:title[\s_-]*update|tu)[\s_-]*(\d{1,3})', re.I)
_TU_NUM_RE = re.compile(r'(\d{1,3})')
_TITLE_ID_RE = re.compile(r'[0-9A-Fa-f]{8}')


def title_update_from_path(path) -> int:
    """
    The title update a save sits under, from its folder path.

    Nexia360 files worlds per title update:
        Library\\<title id>\\Title Update 14\\Content\\<profile>\\<save>\\savegame.dat

    The folder is often renamed, so position wins over wording: the segment
    directly above "Content" is the title update, whatever it is called. Only
    if there is no Content segment does the name itself get matched.
    """
    parts = list(Path(path).parts)
    lowered = [p.lower() for p in parts]
    if 'content' in lowered:
        i = lowered.index('content')
        # Only Nexia360 puts a title update folder above Content, and it always
        # sits under <title id>. Requiring that guards against Xenia, whose
        # content folder is at the top with the emulator's own folder above it.
        if i >= 2 and _TITLE_ID_RE.fullmatch(parts[i - 2]):
            seg = parts[i - 1]
            m = _TU_NUM_RE.search(seg)
            if m:
                return int(m.group(1))
            # The base game folder is named exactly NO_TU. Nothing else
            # unnumbered is assumed to be TU0 - guessing there would label
            # unknown folders with a title update they may not be.
            if seg.strip().upper() == 'NO_TU':
                return 0
            return None
    for part in reversed(parts):
        m = _TU_TEXT_RE.search(part)
        if m:
            return int(m.group(1))
    return None


def peek_stfs_metadata(path):
    """
    Name and thumbnail from a .bin without reading the whole file.

    Everything we need lives in the first 0xA000 bytes, so a folder of 10 MB
    saves can be listed without loading any of them.
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(STFS_BASE_OFFSET)
    except OSError:
        return None, None
    if len(head) < STFS_BASE_OFFSET or head[:4] != STFS_MAGIC:
        return None, None
    try:
        name = head[DISPLAY_NAME_OFF: DISPLAY_NAME_OFF + DISPLAY_NAME_SIZE]
        name = name.decode('utf-16-be').split('\x00')[0].strip()
    except Exception:
        name = None
    return (name or None), read_stfs_thumbnail(head)


def scan_worlds(folder, direction: str, platform: str = 'xbox360') -> list:
    """
    List every world in a folder, cheaply.

    *direction* is the conversion being set up: 'to_console' looks for Windows
    LCE worlds, 'to_win64' looks for console saves. Nothing is decompressed -
    only names and thumbnails are read, so this stays fast on a full GameHDD.

    Returns dicts of {path, name, thumbnail, detail}, sorted by name.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    found = []

    def add(path, name, thumb, detail):
        found.append({'path': str(path), 'name': name or path.name,
                      'thumbnail': thumb, 'detail': detail})

    def win64_world(d):
        """The .ms inside a Windows LCE world folder, or None."""
        ms = d / 'saveData.ms'
        if ms.exists():
            return ms
        loose = sorted(d.glob('*.ms'))
        return loose[0] if len(loose) == 1 else None

    # A folder that is itself a world, rather than a folder of worlds.
    candidates = [root] + sorted(p for p in root.iterdir() if p.is_dir())

    for d in candidates:
        if direction == 'to_console':
            ms = win64_world(d)
            if ms:
                add(ms, read_worldname_txt(d) or d.name,
                    find_thumbnail(d, WIN64_THUMB_REL), 'Windows LCE world')
        elif platform == 'ps3':
            if (d / 'GAMEDATA').exists():
                try:
                    from .converter_ps3 import _parse_param_sfo
                    sfo = _parse_param_sfo(d / 'PARAM.SFO')
                except Exception:
                    sfo = {}
                add(d / 'GAMEDATA', sfo.get('SUB_TITLE') or d.name,
                    find_thumbnail(d, 'THUMB'), 'PS3 save')
        else:
            dat = d / EMU_DAT_NAME
            if dat.exists():
                add(dat, read_worldname_txt(d) or d.name,
                    find_thumbnail(d, EMU_THUMB_NAME), 'emulator world')

    # Loose .bin packages sit directly in the folder, not in subfolders.
    if direction == 'to_win64' and platform != 'ps3':
        for b in sorted(root.glob('*.bin')):
            name, thumb = peek_stfs_metadata(b)
            if name is not None or thumb is not None:
                add(b, name or b.stem, thumb, 'Xbox 360 package')

    seen, unique = set(), []
    for w in found:
        if w['path'] not in seen:
            seen.add(w['path'])
            unique.append(w)
    return sorted(unique, key=lambda w: w['name'].lower())


# =============================================================================
# Java Edition
#
# A Java world is a folder, not a container:
#   <world>/level.dat            gzipped big-endian NBT
#   <world>/icon.png             the world icon, since 1.7
#   <world>/region/r.X.Z.mca     Anvil chunks (McRegion .mcr before 1.2)
#
# LCE and Java share NBT and the region container shape, but not the chunk
# payload: LCE stores 128-tall chunks through its own RLE + platform codec,
# Java stores 256-tall (and later taller) chunks as plain zlib NBT. Reading is
# implemented; translating chunks is not.
# =============================================================================

JAVA_LEVEL_DAT = 'level.dat'
JAVA_REGION_DIRS = ('region', 'DIM-1/region', 'DIM1/region')
JAVA_ICONS = ('icon.png',)


def read_java_level_dat(path):
    """Decompress a Java level.dat to raw NBT. Handles gzip, zlib and plain."""
    data = Path(path).read_bytes()
    if data[:2] == b'\x1f\x8b':
        import gzip
        return gzip.decompress(data)
    if data[:1] == b'\x78':
        return zlib.decompress(data)
    return data


def describe_java_world(folder) -> dict:
    """
    What a Java world is, from level.dat and the region folders.

    Uses the same big-endian NBT helpers as the console formats - Java NBT is
    big-endian too, which is why LCE saves read as NBT without byte swapping.
    """
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    nbt = read_java_level_dat(folder / JAVA_LEVEL_DAT)

    version_name = None
    i = nbt.find(b'\x0a\x00\x07Version')          # TAG_Compound "Version"
    if i >= 0:
        version_name = _nbt_string(nbt[i:i + 200], 'Name')

    dims, regions = [], 0
    for rel, label in zip(JAVA_REGION_DIRS, ('Overworld', 'Nether', 'End')):
        d = folder / rel
        if d.is_dir():
            n = len(list(d.glob('*.mca'))) + len(list(d.glob('*.mcr')))
            if n:
                dims.append(label)
                regions += n

    anvil = any((folder / r).is_dir() and any((folder / r).glob('*.mca'))
                for r in JAVA_REGION_DIRS)

    return {
        'level_name': _nbt_string(nbt, 'LevelName'),
        'version_name': version_name,
        'data_version': _nbt_int_value(nbt, 'DataVersion'),
        'seed': _nbt_long(nbt, 'RandomSeed'),
        'spawn': (_nbt_int_value(nbt, 'SpawnX'), _nbt_int_value(nbt, 'SpawnY'),
                  _nbt_int_value(nbt, 'SpawnZ')),
        'generator_name': _nbt_string(nbt, 'generatorName'),
        'dimensions': dims,
        'region_files': regions,
        'chunk_format': 'Anvil (.mca)' if anvil else 'McRegion (.mcr)',
        'icon': next((folder / n for n in JAVA_ICONS if (folder / n).exists()),
                     None),
    }


def _nbt_int_value(blob: bytes, name: str):
    off = _nbt_int(blob, name)
    return struct.unpack_from('>i', blob, off)[0] if off is not None else None


MAX_SCAN_DEPTH = 8

# Minecraft: Xbox 360 Edition. Read from the titleId field at 0x360 of a retail
# save, and the same value names the folder on a memory unit and under
# Nexia360's Library.
MINECRAFT_TITLE_ID = 0x584111F7
MINECRAFT_TITLE_ID_HEX = f"{MINECRAFT_TITLE_ID:08X}"


def peek_stfs_title_id(path):
    """The title ID of a .bin, from its header alone. None if not a package."""
    try:
        with open(path, 'rb') as f:
            head = f.read(0x364)
    except OSError:
        return None
    if len(head) < 0x364 or head[:4] != STFS_MAGIC:
        return None
    return struct.unpack_from('>I', head, 0x360)[0]


def list_drives() -> list:
    """
    [{'root', 'label', 'has_content'}] for every drive on the machine.

    Every drive, in the order Windows shows them - not only the ones already
    holding console saves. A memory unit that has never been written to has no
    Content folder yet, and hiding it makes the tool look like it cannot see
    the drive at all.
    """
    import string
    out = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        try:
            if not root.is_dir():
                continue
            has = (root / 'Content').is_dir()
        except OSError:
            continue
        drive = {'root': root, 'label': f"{letter}:", 'has_content': has,
                 'volume': _volume_name(root),
                 'explorer_label': _explorer_label(root),
                 'unc': _drive_unc(root),
                 'kind': _drive_type(root)}
        drive['display'] = drive_display_name(drive)
        out.append(drive)
    return out


# What Windows calls each drive type. A letter on its own tells you nothing,
# and plenty of drives have no label at all - a blank memory unit usually
# does not - so the type is what makes the list readable.
DRIVE_TYPES = {2: 'Removable', 3: 'Local Disk', 4: 'Network', 5: 'CD/DVD',
               6: 'RAM Disk'}


# The generic name Windows shows when a drive has no label of its own. A blank
# memory unit or USB stick is the common case and "G: Removable" reads like a
# fault code next to Explorer's "USB Drive (G:)".
GENERIC_DRIVE_NAMES = {'Removable': 'USB Drive', 'Local Disk': 'Local Disk',
                       'Network': 'Network Drive', 'CD/DVD': 'CD Drive',
                       'RAM Disk': 'RAM Disk'}

MOUNTPOINTS2 = (r'Software\Microsoft\Windows\CurrentVersion\Explorer'
                r'\MountPoints2')


def _drive_unc(root) -> str:
    r"""The \\host\share a mapped drive points at, or '' if it is local."""
    try:
        import ctypes.wintypes as wintypes
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        letter = str(root)[:2]
        if ctypes.WinDLL('mpr').WNetGetConnectionW(
                ctypes.c_wchar_p(letter), buf, ctypes.byref(size)) == 0:
            return buf.value
    except Exception:
        pass
    return ''


def _explorer_label(root) -> str:
    r"""
    The name someone typed over a drive in Explorer, or ''.

    Renaming a drive in Explorer does not touch the volume label - it writes
    _LabelFromReg under MountPoints2, keyed by the drive letter for a local
    disk and by the UNC with every backslash turned into a hash for a mapped
    one: \\MYCLOUDEX2ULTRA\Public\Patrick becomes
    ##MYCLOUDEX2ULTRA#Public#Patrick. Without reading it, a NAS the user calls
    "Patrick NAS" lists as the share's volume label instead.
    """
    try:
        import winreg
    except ImportError:
        return ''
    keys = []
    unc = _drive_unc(root)
    if unc:
        keys.append(unc.replace('\\', '#'))
    keys.append(str(root)[:1])
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                MOUNTPOINTS2 + '\\' + key) as k:
                value = winreg.QueryValueEx(k, '_LabelFromReg')[0]
            if value:
                return str(value).strip()
        except OSError:
            continue
    return ''


def drive_display_name(drive: dict) -> str:
    """
    A drive named the way Explorer names it: "USB Drive (G:)".

    In order of authority: the name typed over it in Explorer, then the volume
    label, then a generic name for its type. The letter always comes last and
    in brackets, so the list reads as names rather than as letters.
    """
    root = drive.get('root')
    letter = str(drive.get('label') or (str(root)[:2] if root else '')).rstrip(
        ':') + ':'
    name = drive.get('explorer_label') or drive.get('volume') or ''
    if not name:
        unc = drive.get('unc') or ''
        if unc:
            parts = [p for p in unc.split('\\') if p]
            name = parts[-1] if parts else ''
    if not name:
        name = GENERIC_DRIVE_NAMES.get(drive.get('kind') or '', 'Drive')
    return f'{name} ({letter})'


def _volume_name(root) -> str:
    """A drive's label, or '' if it has none."""
    try:
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(str(root)), buf, 261,
            None, None, None, None, 0)
        return buf.value if ok else ''
    except Exception:
        return ''


def _drive_type(root) -> str:
    """'Removable', 'Network', ... or '' if Windows will not say."""
    try:
        n = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(str(root)))
        return DRIVE_TYPES.get(n, '')
    except Exception:
        return ''


def drive_caption(d: dict) -> str:
    """
    How one drive should read in a list: 'G:  SANDISK', 'Z:  Network'.

    The label when it has one, the type when it does not. A memory unit
    straight out of a console usually has neither a label nor a Content
    folder yet, so falling back to the letter alone would leave a row that
    says nothing at all.
    """
    name = d.get('volume') or d.get('kind') or ''
    return f"{d['label']}  {name}".strip()


def list_content_drives() -> list:
    """
    Drives holding a Content folder - a memory unit, USB stick or external HDD.

    A console lays saves out as:
        <drive>\\Content\\<device id>\\<title id>\\<save id>\\<file>

    Checking for the folder rather than the drive type also picks up external
    hard drives, which Windows reports as fixed.
    """
    import string
    roots = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        try:
            if (root / 'Content').is_dir():
                roots.append(root)
        except OSError:
            continue
    return roots


def scan_holding_folder(root) -> list:
    """
    Worlds parked on a drive by import, outside any console layout.

    They are listed because the tool put them there and hiding them would be
    a lie, but they are labelled so it is obvious they are not console-ready.
    """
    holding = Path(root) / IMPORT_HOLDING_FOLDER
    if not holding.is_dir():
        return []
    found = []
    for child in sorted(holding.iterdir()):
        hit = None
        if child.is_dir() and (child / EMU_DAT_NAME).is_file():
            hit = child / EMU_DAT_NAME
        elif child.is_file() and child.suffix.lower() == '.bin':
            hit = child
        if not hit:
            continue
        folder = hit.parent if hit.name == EMU_DAT_NAME else holding
        name = (read_worldname_txt(folder) if hit.name == EMU_DAT_NAME
                else None) or child.stem
        tu, tu_text = confirm_title_update(hit, None)
        found.append({
            'path': str(hit), 'name': name,
            'thumbnail': find_thumbnail(folder, EMU_THUMB_NAME),
            'detail': 'imported', 'title_update': tu, 'tu_range': tu_text,
            'profile_name': None, 'profile_xuid': None, 'profile_pic': None,
            'source': 'removable', 'platform': 'xbox360',
            'console_ready': False,
        })
    return found


def scan_removable_saves(roots=None) -> list:
    """
    Minecraft saves on attached drives - all of them, or just *roots*.

    A .bin is accepted on its title ID, which is what the file actually says,
    rather than on where it sits. A bare savegame.dat has no header to check,
    so it is accepted when the Minecraft title ID names one of its folders.
    """
    found = []
    for root in (roots if roots is not None else list_content_drives()):
        root = Path(root)
        content = root / 'Content'
        for pattern in ('*.bin', 'savegame.dat'):
            try:
                hits = [p for p in content.rglob(pattern)
                        if len(p.relative_to(content).parts) <= 6]
            except OSError:
                continue
            for hit in sorted(hits):
                if hit.suffix.lower() == '.bin':
                    if peek_stfs_title_id(hit) != MINECRAFT_TITLE_ID:
                        continue
                    name, thumb = peek_stfs_metadata(hit)
                    name = name or hit.stem
                else:
                    parts = [p.upper() for p in hit.parts]
                    if MINECRAFT_TITLE_ID_HEX not in parts:
                        continue
                    thumb = find_thumbnail(hit.parent, EMU_THUMB_NAME)
                    name = read_worldname_txt(hit.parent) or hit.parent.name
                # A world on a drive sits under its owner's XUID, and the
                # profile package is often right beside it - so the gamertag
                # is available here just as it is for an emulator.
                xuid = profile_id_from_path(hit)
                owner = read_usb_profile(root, xuid) if xuid else {}
                confirmed_tu, tu_text = confirm_title_update(
                    hit, title_update_from_path(hit))
                found.append({
                    'path': str(hit), 'name': name, 'thumbnail': thumb,
                    'detail': f"USB ({root.drive or root})",
                    'title_update': confirmed_tu,
                    'tu_range': tu_text,
                    'profile_name': owner.get('gamertag') or owner.get('xuid'),
                    'profile_xuid': owner.get('xuid'),
                    'profile_pic': owner.get('picture'),
                    'source': 'removable', 'platform': 'xbox360',
                })
    return found


def quick_save_version(path):
    """
    The save version of a world, without decoding anything.

    Only the 12-byte payload header is needed, so this is fast enough to run
    over a whole folder while building a list.
    """
    try:
        payload, _name, _thumb = read_console_input(path, 'xbox360')
        return parse_payload(payload, '>')[3]
    except Exception:
        return None


def tu_range_for(path):
    """
    'TU46 - TU68' for a world with no exact title update, or ''.

    Uses the save version for the range and level.dat's fields to raise the
    floor, so this is as tight as the save itself allows.
    """
    try:
        payload, _n, _t = read_console_input(path, 'xbox360')
        return title_update_text(payload, '>')
    except Exception:
        return ''


def world_dates(path) -> dict:
    """
    {'created', 'modified'} as epoch seconds, or None where unknown.

    st_ctime is the creation time on Windows, which is what is wanted here -
    on Unix it is the inode change time and would read as "modified" twice
    over, so it is only trusted when it is not later than the modification
    time.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return {'created': None, 'modified': None}
    made = getattr(st, 'st_birthtime', None) or st.st_ctime
    if made and made > st.st_mtime + 1:
        made = st.st_mtime
    return {'created': made or None, 'modified': st.st_mtime or None}


def _parse_history_date(text):
    """'August 7, 2026, 4:07 PM' back to epoch seconds, or None."""
    if not text:
        return None
    import datetime
    try:
        return datetime.datetime.strptime(
            text.strip(), '%B %d, %Y, %I:%M %p').timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def world_dates_for(folder) -> dict:
    """
    A world's dates, with the history overruling the filesystem.

    A conversion writes a brand new file, so the copy's creation date is the
    date of the conversion - not of the world. Where the history recorded the
    real one, that is the answer; the filesystem only fills in for worlds
    this tool has never touched.
    """
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    dates = world_dates(_save_file_in(folder) or folder)
    recorded = _parse_history_date(read_history(folder)[0].get(
        'Original created'))
    if recorded:
        dates['created'] = recorded
    return dates


def format_world_date(when) -> str:
    """
    'August 7, 2026, 4:07 PM', or '' if there is none.

    Month spelled out and the day without a leading zero, because the column
    is read rather than scanned - and with the time, since two saves of the
    same world on the same day are the normal case, not the exception.

    %-d and %-I are not portable to Windows, so the zeros are stripped by
    hand rather than by a format flag.
    """
    if not when:
        return ''
    try:
        import datetime
        t = datetime.datetime.fromtimestamp(when)
    except (OSError, OverflowError, ValueError):
        return ''
    hour = t.hour % 12 or 12
    return (f'{t.strftime("%B")} {t.day}, {t.year}, '
            f'{hour}:{t.strftime("%M %p")}')


# How long each unit is, largest first, in seconds. Months and years are the
# average Gregorian ones - a list column saying "3 months ago" does not want
# calendar arithmetic behind it, it wants to be about right and never wrong
# by a unit.
AGO_UNITS = (('year', 365.2425 * 86400), ('month', 30.436875 * 86400),
             ('week', 7 * 86400), ('day', 86400), ('hour', 3600),
             ('minute', 60))


def format_time_ago(when, now=None) -> str:
    """
    '3 days ago', or 'just now' for anything under a minute.

    Singular and plural both appear in a world list, so "1 day ago" has to
    read as well as "2 days ago".
    """
    if not when:
        return ''
    import time as _time
    delta = (now if now is not None else _time.time()) - when
    if delta < 0:
        return 'just now'          # a clock skew, not a world from the future
    for name, size in AGO_UNITS:
        n = int(delta // size)
        if n >= 1:
            return f'{n} {name} ago' if n == 1 else f'{n} {name}s ago'
    return 'just now'


def format_world_when(when, now=None) -> str:
    """
    '3 days ago (August 7, 2026, 4:07 PM)'.

    How long ago first, because that is the part anyone actually compares
    worlds by; the exact date in brackets for when it matters.
    """
    exact = format_world_date(when)
    if not exact:
        return ''
    return f'{format_time_ago(when, now)} ({exact})'


def scan_source(root, source_key: str) -> list:
    """
    Every world under a configured game or emulator folder.

    Searches the source's known subpath, falling back to the folder itself so
    it still works if someone points straight at GameHDD or content. Nothing is
    decompressed - names, thumbnails and title updates all come from the file
    tree and small side files.
    """
    spec = WORLD_SOURCES.get(source_key)
    root = Path(root)
    if not spec or not root.is_dir():
        return []

    bases = []
    for sub in spec['subpaths']:
        p = root / sub if sub else root
        if p.is_dir() and p not in bases:
            bases.append(p)
        if bases and sub:
            break                      # the known layout matched; stop guessing

    target = spec['target']
    found, seen = [], set()

    for base in bases:
        if spec['recursive']:
            try:
                hits = [p for p in base.rglob(target)
                        if len(p.relative_to(base).parts) <= MAX_SCAN_DEPTH]
            except OSError:
                hits = []
        else:
            hits = [d / target for d in base.iterdir()
                    if d.is_dir() and (d / target).exists()]
            if (base / target).exists():
                hits.append(base / target)
            if not hits:                     # a loose *.ms world folder
                for d in base.iterdir():
                    if d.is_dir():
                        loose = sorted(d.glob('*.ms'))
                        if len(loose) == 1:
                            hits.append(loose[0])

        for hit in sorted(hits):
            if hit in seen:
                continue
            seen.add(hit)
            folder = hit.parent
            tu = title_update_from_path(hit)

            if spec['platform'] == 'java':
                try:
                    name = _nbt_string(read_java_level_dat(hit), 'LevelName')
                except Exception:
                    name = None
                name = name or folder.name
                was = history_last_known_name(folder)
                if was and was != name:
                    note_rename(folder, was, name)
                icon = folder / 'icon.png'
                thumb = (icon.read_bytes()
                         if icon.exists() and icon.read_bytes()[:8] == PNG_MAGIC
                         else None)
            elif spec['platform'] == 'windows_lce':
                # History first - it is where the name is kept now. worldname
                # .txt is only read for worlds written before it was dropped.
                name = (read_history(folder)[0].get('World name')
                        or read_worldname_txt(folder) or folder.name)
                thumb = find_thumbnail(folder, WIN64_THUMB_REL)
                # No account exists here - the only name anywhere is the one
                # a shortcut launches the game with. Looked up once per scan,
                # not once per world.
                if 'lce_player' not in locals():
                    lce_player = player_name_from_shortcuts(root)
                owner = {'gamertag': lce_player} if lce_player else {}
                # Windows LCE shipped once, at the TU19 feature set - there
                # are no title updates to tell apart, so the column should
                # say so rather than sit empty.
                tu = WINDOWS_LCE_TU
            elif spec['platform'] == 'ps3':
                try:
                    from .converter_ps3 import _parse_param_sfo
                    name = _parse_param_sfo(folder / 'PARAM.SFO').get('SUB_TITLE')
                except Exception:
                    name = None
                name = name or folder.name
                was = history_last_known_name(folder)
                if was and was != name:
                    note_rename(folder, was, name)
                thumb = find_thumbnail(folder, 'THUMB')
            else:
                # An emulator save's real name is in _MinecraftSaveInfo beside
                # it, NOT in the folder name - those only match while nobody
                # has renamed anything. Fall back to the folder when there is
                # no index, which is what happens for a hand-made folder.
                # The GAME's own records first, ours last. _MinecraftSaveInfo
                # and the .header are what the console reads; worldname.txt is
                # a side file this tool writes. Reading ours first meant one
                # stale worldname.txt - left by an early conversion that wrote
                # the folder name into it - outranked an index and a header
                # that both said "New World", and the world listed as
                # "Save20260807154226.bin" forever after.
                name = None
                thumb = find_thumbnail(folder, EMU_THUMB_NAME)
                for entry in read_saveinfo(folder.parent):
                    if entry['folder'].lower() == folder.name.lower():
                        name = entry['name']
                        thumb = thumb or entry['thumbnail']
                        break
                # The index and the header are written independently, so a
                # base game (NO_TU) library has headers and no index at all.
                if not name:
                    name = read_save_header_name(folder.parent, folder.name)
                if not name:
                    name = read_worldname_txt(folder)
                name = name or folder.name
                was = history_last_known_name(folder)
                if was and was != name:
                    note_rename(folder, was, name)

            from_history = history_title_update(folder)
            tu_text = ''
            from_path = tu
            # For an EMULATOR world the folder is not a label - it is the
            # install location, and the game loads the world as whatever
            # "Title Update N" folder it sits in. So that number is exact and
            # authoritative here; showing a save-derived range like "TU69-TU75"
            # instead is just wrong, because the game does not read the save to
            # decide the version, it reads the folder. History wins over even
            # that, being our own exact record.
            if from_history is not None:
                tu = from_history
            elif source_key in ('nexia360', 'xenia') and from_path is not None:
                tu = from_path
            elif spec['platform'] in ('xbox360', 'ps3'):
                # A loose .bin or a drive world has no install folder, so the
                # save is the only evidence - and it may only give a range.
                tu, tu_text = confirm_title_update(hit, tu)
            # The gamertag is not under the world - Nexia keeps saves in
            # Library/ and accounts in content/ - so resolve it from the
            # install root by XUID rather than by walking up the path.
            xuid = profile_id_from_path(hit)
            if spec['platform'] != 'windows_lce':
                owner = {'xuid': xuid}
                if xuid:
                    for prof in _profiles_cached(root):
                        if prof['xuid'].upper() == xuid.upper():
                            owner = prof
                            break
            found.append({
                # A raw XUID is not a name. It shows up when a world is filed
                # under a profile that does not exist in THIS install - a
                # world copied in from another Nexia folder, say - and
                # "B13EBABEBABEBABE" in the Profile column reads as a bug
                # rather than as "nobody here owns this".
                'profile_name': owner.get('gamertag') or (
                    UNKNOWN_PROFILE if owner.get('xuid') else None),
                'profile_xuid': owner.get('xuid'),
                'profile_pic': owner.get('picture'),
                'path': str(hit), 'name': name, 'thumbnail': thumb,
                'detail': spec['detail'],
                'title_update': from_history if from_history is not None else tu,
                'tu_from_history': from_history is not None,
                'tu_from_path': from_path,
                'tu_range': tu_text,
                'original': history_original(folder),
                'source': source_key, 'platform': spec['platform'],
                # When the world was made and when it was last played. From
                # the save rather than the folder, and from the history rather
                # than the save where the history knows better - a converted
                # world's file was created by the conversion, not by whoever
                # made the world.
                **world_dates_for(hit),
                # Which configured folder this came from. With more than one
                # install of the same emulator - a testing one beside the one
                # being played on - the source key alone no longer says where
                # a world lives or where a converted one should go back to.
                'source_root': str(root),
            })

    return found


# Where each emulator expects a world to sit. A save id folder is created
# inside these; the profile folder is reused when one already exists so the
# world lands beside the others rather than under an invented profile.
DEFAULT_PROFILE_ID = 'B13EBABEBABEBABE'
DEFAULT_SAVE_ID = '00000001'

# Shown when a world's profile folder has no Account file in this install.
UNKNOWN_PROFILE = 'Unknown profile'


NEXIA_CONFIG_NAMES = ('nexia360.config.toml', 'nexia360.toml', 'config.toml')
_XUID_RE = re.compile(r'logged_profile_slot_0_xuid\s*=\s*"([^"]*)"')


def nexia_profile_xuid(root):
    """
    The XUID Nexia360 loads on boot, from nexia360.config.toml.

        [Profiles]
        logged_profile_slot_0_xuid = "B13EBABEBABEBABE"

    A world has to sit under that profile's folder or the emulator will not
    list it - guessing from whatever folders happen to exist picks the wrong
    one as soon as there is more than a single profile.
    """
    root = Path(root)
    for name in NEXIA_CONFIG_NAMES:
        p = root / name
        if not p.exists():
            continue
        try:
            m = _XUID_RE.search(p.read_text('utf-8', errors='replace'))
        except OSError:
            continue
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _pick_profile_folder(parent: Path, preferred: str = None) -> Path:
    """
    Where a world should go: the named profile, an existing one, or a default.
    """
    if preferred:
        return parent / preferred
    if parent.is_dir():
        for child in sorted(parent.iterdir()):
            if child.is_dir() and len(child.name) >= 8:
                return child
    return parent / DEFAULT_PROFILE_ID


_SAVE_ID_RE = re.compile(r'\d{8}')


# What a world folder is called when the world has no name to use.
DEFAULT_WORLD_FOLDER = 'World'


def _save_id_folder(profile: Path) -> Path:
    """
    The eight-digit save id a profile keeps its worlds under.

    This level is not decoration. The game reads _MinecraftSaveInfo from
    inside it, so a world written one level up is filed where nothing will
    look for its name - which is how a converted world ends up showing the
    folder it happens to sit in instead of what it is called. An existing
    save id is reused so worlds stay together.
    """
    if profile.is_dir():
        ids = sorted(c.name for c in profile.iterdir()
                     if c.is_dir() and _SAVE_ID_RE.fullmatch(c.name))
        if ids:
            return profile / ids[0]
    return profile / DEFAULT_SAVE_ID


SAVE_FOLDER_SUFFIX = '.bin'


def _bin_name(base: str) -> str:
    """The save folder name, with the .bin every real library uses."""
    return base if base.lower().endswith(SAVE_FOLDER_SUFFIX) \
        else base + SAVE_FOLDER_SUFFIX


def _save_folder_for(profile: Path, world_name: str = None) -> Path:
    """
    The folder a world goes in: <profile>/<save id>/<world>.

    A console names the world folder with a timestamp, which tells you nothing
    about which world it holds - and a bare savegame.dat stores no name either.
    Naming it after the world makes it findable; duplicates get (1), (2)
    rather than overwriting. The display name still comes from the index.

    The name always ends in .bin. Every save in a real Nexia or Xenia library
    does - the emulators keep a world as a FOLDER named like the .bin package
    the console would have written - and the header records that exact name.
    """
    save_id = _save_id_folder(profile)
    if world_name:
        base = _bin_name(sanitise(world_name))
        cand = save_id / base
        n = 1
        while cand.exists():
            cand = save_id / _bin_name(f"{sanitise(world_name)} ({n})")
            n += 1
        return cand

    if not save_id.is_dir():
        return save_id / _bin_name(DEFAULT_WORLD_FOLDER)
    used = {c.name.lower() for c in save_id.iterdir() if c.is_dir()}
    for n in range(1, 1000):
        name = _bin_name(f"{DEFAULT_WORLD_FOLDER} ({n})" if n > 1
                         else DEFAULT_WORLD_FOLDER)
        if name.lower() not in used:
            return save_id / name
    return save_id / _bin_name(DEFAULT_WORLD_FOLDER)


def emulator_destination(root, kind: str, title_update: int = 19,
                         world_name: str = None, xuid: str = None) -> Path:
    """
    The folder an emulator expects a world in.

        Nexia360  <root>\\Library\\<title id>\\Title Update N\\Content\\<profile>\\<save>
        Xenia     <root>\\content\\<profile>\\<title id>\\<save>
        USB       <drive>\\Content\\<device>\\<title id>\\<save>

    Nexia360 files worlds per title update, so a converted world has to go in
    the folder for the version it is - Windows LCE is CU7 / TU19.
    """
    root = Path(root)
    if kind == 'nexia360':
        # Whichever existing folder really holds this update - NOT simply the
        # first one whose name parses, which is how a world ends up in an
        # empty "Title Update 19" while the game boots "Title Update 19-2".
        tu_dir = nexia_title_update_folder(root, title_update)
        if tu_dir is None:
            # REFUSE rather than create it. Nexia will not install an update
            # into a folder that already exists - it makes a second one and
            # leaves this one behind - so creating the folder here guarantees
            # the world is stranded the moment the update is installed. The
            # only safe order is: install the update first, then convert.
            raise TitleUpdateNotInstalled(title_update, root)
        profile = _pick_profile_folder(tu_dir / 'Content',
                                       xuid or nexia_profile_xuid(root))
    elif kind == 'xenia':
        content = root / 'content'
        profile = _pick_profile_folder(content, xuid) / MINECRAFT_TITLE_ID_HEX
    else:                                            # a memory unit or USB
        content = root / 'Content'
        profile = _pick_profile_folder(content) / MINECRAFT_TITLE_ID_HEX
    return _save_folder_for(profile, world_name)


def source_directories(directories: dict, key: str) -> list:
    """
    Every folder configured for one source, in order.

    A setting used to be one path and is now a list - someone can keep a
    testing emulator install beside the one they actually play on. Both shapes
    are read so an existing settings.json still works; a list is what gets
    written back.
    """
    value = (directories or {}).get(key)
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def scan_configured_sources(platform: str, directories: dict,
                            include_drives: bool = True) -> list:
    """
    Worlds from every configured source that targets *platform*.

    Attached drives are searched too and need no setting - a memory unit gets a
    different letter every time it is plugged in, so there is nothing stable to
    configure.
    """
    out = []
    for key, spec in WORLD_SOURCES.items():
        if spec['platform'] != platform:
            continue
        for root in source_directories(directories, key):
            out.extend(scan_source(root, key))
    if include_drives and platform == 'xbox360':
        out.extend(scan_removable_saves())

    seen, unique = set(), []
    for w in out:
        if w['path'] not in seen:
            seen.add(w['path'])
            unique.append(w)
    unique.sort(key=world_sort_key)
    return unique


def world_sort_key(w: dict):
    """
    Oldest first, with worlds we could not date at all last.

    A world whose exact title update is unknown still usually has a range from
    its save version, and the bottom of that range places it about right -
    which is far more useful than dumping every undated world at the end.
    "We could not tell" is not TU0, so a world with nothing at all goes last.
    """
    tu = w.get('title_update')
    if tu is None:
        span = w.get('tu_range') or ''
        found = re.findall(r'TU(\d+)', span)
        tu = int(found[0]) if found else None
    return (tu is None, tu or 0, (w.get('name') or '').lower())


def read_console_input(path, platform: str):
    """
    Load a console save from any supported shape.

    Returns (be_payload, world_name, thumbnail_png_or_None).
    Accepts an Xbox 360 .bin, an emulator folder (savegame.dat +
    __thumbnail.png), a bare savegame.dat, or a PS3 save folder.
    """
    import sys
    pass  # vendored: sys.path hack removed
    p = Path(path)

    # Selecting the save file inside a world folder means the folder itself -
    # that is where the name and the thumbnail live.
    if p.is_file() and p.name.lower() in ('savegame.dat', 'gamedata'):
        p = p.parent

    if platform == 'ps3':
        from .converter_ps3 import _parse_param_sfo
        src = p if p.is_dir() else p.parent
        gamedata = src / 'GAMEDATA'
        if not gamedata.exists():
            raise RuntimeError(
                f"GAMEDATA not found in {src.name}. Pick the PS3 save folder "
                "(it contains GAMEDATA, PARAM.SFO and THUMB).")
        payload = gamedata.read_bytes()
        sfo = _parse_param_sfo(src / 'PARAM.SFO')
        name = sfo.get('SUB_TITLE') or src.name
        return payload, name, find_thumbnail(src)

    # ---- Xbox 360 ----
    if p.is_dir():                                    # emulator world folder
        dat_path = p / EMU_DAT_NAME
        if not dat_path.exists():
            raise RuntimeError(
                f"{EMU_DAT_NAME} not found in {p.name}. An emulator world "
                f"folder contains {EMU_DAT_NAME} and {EMU_THUMB_NAME}.")
        name = read_worldname_txt(p) or p.name
        return read_savegame_dat(dat_path.read_bytes()), name, find_thumbnail(p)

    raw = p.read_bytes()
    if raw[:4] == STFS_MAGIC:                         # STFS .bin
        from .converter import STFSPackage
        pkg = STFSPackage(raw)
        dat = pkg.extract_savegame_dat()
        if dat is None:
            raise RuntimeError(
                "savegame.dat not found in the STFS package. Make sure this "
                "is a valid Xbox 360 Minecraft LCE save.")
        # A .bin is self-contained: trust its embedded thumbnail over anything
        # that happens to be sitting in the same folder.
        return (read_savegame_dat(dat), pkg.display_name,
                read_stfs_thumbnail(raw) or pkg.thumbnail
                or find_thumbnail(p.parent))

    return read_savegame_dat(raw), world_name_for(p), find_thumbnail(p.parent)


# =============================================================================
# Console -> Win64
# =============================================================================

# LCE block / item / entity / biome ids, extracted from the reference tools
# under Resources/ and cross-checked against known legacy Minecraft ids.
# Data only - Minecraft's own numbering, no code from anywhere.
IDS_PATH = TEMPLATES_DIR / 'lce_ids.json'
_IDS = None


def id_tables() -> dict:
    """The id tables, loaded once. {} if the file is missing."""
    global _IDS
    if _IDS is None:
        try:
            import json
            _IDS = json.loads(IDS_PATH.read_text('utf-8'))
        except Exception:
            _IDS = {'blocks': {}, 'items': {}, 'entities': {},
                    'tile_entities': {}, 'biomes': {}}
    return _IDS


def block_name(block_id: int, data: int = 0):
    """
    Name a legacy block id, or None if it is not in the table.

    An exact id:data is tried first, then the bare id - the table stores a
    bare id when every data value means the same block, and only splits it out
    where the data value changes what the block is.
    """
    blocks = id_tables().get('blocks', {})
    hit = blocks.get(f"{block_id}:{data}") or blocks.get(str(block_id))
    return hit['name'] if hit else None


def item_name(item_id: int, damage: int = 0):
    """Name a legacy item id, or None."""
    items = id_tables().get('items', {})
    return items.get(f"{item_id}:{damage}") or items.get(str(item_id))


def java_target_for(title_update=None, save_version=None) -> str:
    """
    Which Java Edition a world should be converted to.

    The title update decides it when we know it - TU19 is Java 1.6.4 - because
    that is the version the world's blocks and items actually came from.
    Without one, the save version narrows it to a range and the newest update
    in that range is used, which is the safest guess: a world converted to a
    slightly newer Java loads, one converted to an older one may not.

    Never returns a pre-Anvil version, because Anvil is what gets written.
    """
    from . import lce_java
    version = java_version_for(title_update) if title_update is not None else None
    if version is None and save_version is not None:
        span = SAVE_VERSION_SHORT.get(save_version, '')
        hits = [int(n) for n in re.findall(r'TU(\d+)', span)]
        if hits:
            version = java_version_for(max(hits))
    if version is None:
        version = '1.6.4'                      # TU19, the commonest case

    order = list(lce_java.JAVA_VERSIONS)
    if version not in order:
        return lce_java.OLDEST_ANVIL_JAVA

    # Two hard bounds, both set by what this converter actually writes:
    #   * below Anvil (Java 1.2) the output would have to be McRegion, and it
    #     is not - so never claim to be older than that.
    #   * from Java 1.13 blocks are namespaced states, and the output is
    #     numeric ids - so never claim to be newer than the last version that
    #     reads numeric ids, whatever the title update says.
    lo = order.index(lce_java.OLDEST_ANVIL_JAVA)
    hi = max(i for i, v in enumerate(order)
             if not lce_java.JAVA_VERSIONS[v][1])
    return order[min(max(order.index(version), lo), hi)]


def convert_lce_to_java(src, platform='xbox360', out_dir=None, log=None,
                        world_name=None, java_version=None,
                        title_update=None):
    """
    An LCE world -> a Java Edition world folder (level.dat + region/*.mca).

    Takes a console save or a Windows LCE one; the only difference to us is
    that the Windows container is little-endian and its chunks are zlib rather
    than LZX.

    Only TU17 - TU25 worlds can be read: those store chunks in the "compressed
    tile storage" layout, which is the one that has been worked out. Anything
    else raises with a message naming the format rather than a bare number.
    """
    from . import lce_java
    import sys
    pass  # vendored: sys.path hack removed
    install_chunk_decoder()

    def out(msg=''):
        (log or print)(msg)

    if platform == 'windows_lce':
        ms = find_win64_save(src)
        payload, endian = read_savedata_ms(ms), '<'
        name = read_worldname_txt(Path(ms).parent) or Path(ms).parent.name
    else:
        payload, name, _thumb = read_console_input(src, platform)
        endian = '>'
    if world_name:
        name = world_name
    name = (name or '').strip() or 'MinecraftSave'

    info = describe_world(payload, endian, platform=platform)
    if not java_version:
        java_version = java_target_for(title_update, info['save_version'])

    out(f"  World        : {name}")
    out(f"  Save version : {info['save_version']}  "
        f"({save_version_short(info['save_version'])})")
    out(f"  Chunk format : {info['chunk_format']}")
    out(f"  Seed         : {info['seed']}")
    out(f"  Java target  : {java_version}"
        + (f"  (from TU{title_update})" if title_update is not None
           else "  (from the save version)"))
    out()

    dst = Path(out_dir) if out_dir else OUTPUT_DIR / sanitise(name)
    out("Converting chunks to Anvil ...")
    report = lce_java.convert_world(payload, dst, name, info, log=out,
                                    engine=sys.modules[__name__],
                                    endian=endian, java_version=java_version)

    out()
    out(f"  Chunks       : {report['chunks']:,} converted")
    if report['skipped']:
        out(f"  Skipped      : {report['skipped']:,} "
            f"({'; '.join(report['reasons'])})")
    out(f"  Region files : {report['regions']}")
    out(f"  Written as   : Java {report['java_version']}"
        + (f", DataVersion {report['data_version']}"
           if report['data_version'] else ", no DataVersion (pre-1.9)"))
    out()
    out(f"Done!  ->  {dst}")
    append_history(dst, describe_edition(platform, title_update),
                   describe_edition('java', java_version=report['java_version']),
                   source_folder=src, world_name=name)
    out(f"Drop this folder into .minecraft/saves and open it with Java "
        f"{report['java_version']} or newer.")
    return str(dst)


def convert_console_to_win64(src, platform='xbox360', out_dir=None, log=None,
                             world_name=None, expand=None):
    """
    Console save -> a Win64 world folder (saveData.ms + thumbnails/).

    Region conversion reuses the proven forward converters.

    expand  optional 'small' | 'medium' | 'large'. A console world is always
            Classic; expanding widens the border in level.dat so the Win64
            build generates new terrain beyond the original 864x864. Existing
            chunks are untouched - they already sit inside every larger size.
    """
    import sys
    pass  # vendored: sys.path hack removed
    install_chunk_decoder()          # multi-frame chunks, or TileEntities are lost
    from .converter import _convert_region

    def out(msg=''):
        (log or print)(msg)

    payload, name, thumb = read_console_input(src, platform)
    if world_name:
        name = world_name
    name = (name or '').strip() or 'MinecraftSave'
    out(f"  World        : {name}")
    out(f"  Payload      : {len(payload):,} bytes")

    fto, ne, ov, cv, entries = parse_payload(payload, '>')
    out(f"  Entries      : {ne}   version: original={ov} save={cv}")

    detected = detect_world_size(payload, '>')
    out(f"  World size   : {detected['label']} - {detected['blocks']} x "
        f"{detected['blocks']} blocks ({detected['source']})")
    if expand:
        if expand not in WORLD_SIZES:
            raise ValueError(f"unknown world size {expand!r}")
        if WORLD_SIZES[expand][1] < detected['chunks']:
            raise ValueError(
                f"cannot expand to {WORLD_SIZES[expand][0]}: this world is "
                f"already {detected['chunks']} chunks across.")

    out(f"Converting files  {'PS3' if platform == 'ps3' else 'Xbox 360'} "
        f"(BE) -> Windows LCE (LE) ...")
    blobs = []
    for e in entries:
        fn = e['filename']
        s, l = e['start_offset'], e['length']
        if s + l > len(payload):
            raise RuntimeError(f"Entry {fn!r} runs past the end of the payload.")
        blob = payload[s: s + l]
        if fn.lower() == 'level.dat' and blob and expand:
            blob = _expand_level_dat(blob, expand, out)
        if fn.lower().endswith(MCR_EXT) and blob:
            before = len(blob)
            if platform == 'ps3':
                from .converter_ps3 import _convert_region_ps3
                blob = _convert_region_ps3(blob)
            else:
                blob = _convert_region(blob)
            out(f"  Region  : {fn:<28} {before:>9,} -> {len(blob):>9,}")
        else:
            out(f"  File    : {fn:<28} {len(blob):>9,}")
        blobs.append(blob)

    # The Win64 build expects save version 9, matching the forward converter,
    # but never claim a version older than the build that made the world.
    le_payload = build_payload(ov, max(9, ov), entries, blobs, endian='<')
    out(f"  LE payload   : {len(le_payload):,} bytes")

    # A native Win64 world folder: saveData.ms + worldname.txt + thumbnails/
    dst = Path(out_dir or OUTPUT_DIR) / sanitise(name)
    dst.mkdir(parents=True, exist_ok=True)

    (dst / 'saveData.ms').write_bytes(write_savedata_ms(le_payload))
    out("  saveData.ms  [ok]")

    # The name is not written to a side file. It lives in the history, which
    # the scan reads - one store, so it cannot go stale against another.
    thumb_path = dst / WIN64_THUMB_REL
    thumb_path.parent.mkdir(exist_ok=True)
    thumb_path.write_bytes(thumb or _placeholder_png())
    out(f"  {WIN64_THUMB_REL.as_posix()}  [ok]"
        + ("" if thumb else "  (placeholder - source had no thumbnail)"))

    append_history(dst, describe_edition(platform),
                   describe_edition('windows_lce'), source_folder=src,
                   world_name=name)
    out()
    out(f"Done!  ->  {dst}")
    out("Copy this folder into your Windows LCE saves directory.")
    return str(dst)


# =============================================================================
# Win64 -> console
# =============================================================================

# A retail TU19 world decompresses to roughly 17 MB. Well past that, the
# console is being asked for an allocation it very likely cannot serve.
LARGE_PAYLOAD_WARN = 64 * 1024 * 1024


def check_compatibility(ov: int, cv: int, target_cv: int, platform: str) -> list:
    """
    Reasons this world may not load on the target console.

    This used to warn that save version 8+ ("compressed chunk storage") could
    not be read by TU19. That warning was wrong, and it came from reading the
    ESaveVersions comment rather than from testing:

      - Windows LCE is Xbox One CU7, which shipped the same day as TU19, so a
        Windows LCE world is the same game version as a TU19 world. Only the
        save-format number differs - that lineage reached 9 while Xbox 360
        stopped at 6.
      - CompressedTileStorage exists in the TU19 source, so v8 saving chunk
        storage directly is not a format TU19 lacks.
      - A version 9 world converted to Xbox 360 loads. The earlier crash was
        world size, which cropping fixed.

    Size is handled by cropping rather than refusing, so check_size reports on
    the result instead.
    """
    return []


def check_size(payload_len: int) -> list:
    """Warn if the cropped world is still far larger than a retail one."""
    if payload_len <= LARGE_PAYLOAD_WARN:
        return []
    return [f"Even after cropping, this world is "
            f"{payload_len / 1024 / 1024:.0f} MB. A retail TU19 world is "
            f"around 17 MB. The console loads the whole save into a single "
            f"allocation before reading any of it, so a world this large may "
            f"still fail to load on hardware."]


def convert_win64_to_console(src, platform='xbox360', out_path=None,
                             emulator=False, template_bin=None, log=None,
                             save_version=None, verify=True, world_name=None,
                             warnings=None, profile_id=None):
    """
    Win64 world -> console save.

    platform  'xbox360' or 'ps3'
    emulator  Xbox 360: write a Xenia/Nexia360 folder instead of a .bin.
              PS3 output is always a folder, so this is ignored there.
    profile_id  whose gamertag the .bin is filed under. Left out, it is taken
              from the destination path when that is a Content\\<id>\\ folder,
              and otherwise inherited from the template.
    warnings  optional list; compatibility problems found before any work
              starts are appended to it, so a caller can surface them rather
              than leave them buried in the log.
    """
    def out(msg=''):
        (log or print)(msg)

    ms_path = find_win64_save(src)
    name = sanitise(world_name) if world_name else world_name_for(ms_path)

    out(f"Reading  {ms_path.name} ...")
    payload = read_savedata_ms(ms_path)
    out(f"  Decompressed : {len(payload):,} bytes")

    fto, ne, ov, cv, entries = parse_payload(payload, '<')
    out(f"  Entries      : {ne}   version: original={ov} save={cv}")

    detected = detect_world_size(payload, '<')
    out(f"  World size   : {detected['label']} - {detected['blocks']} x "
        f"{detected['blocks']} blocks ({detected['source']})")
    if detected['chunks'] > WORLD_SIZES['classic'][1]:
        out(f"  World size   : cropping to Classic "
            f"({WORLD_SIZES['classic'][1] * BLOCKS_PER_CHUNK} x "
            f"{WORLD_SIZES['classic'][1] * BLOCKS_PER_CHUNK}) for the console")

    template_raw = None
    target_cv = save_version
    if platform == 'xbox360' and template_bin:
        template_raw = Path(template_bin).read_bytes()
        if target_cv is None:
            try:
                tpl = read_savegame_dat(
                    _extract_dat_from_template(template_raw))
                target_cv = struct.unpack_from('>h', tpl, 10)[0]
                out(f"  Template     : saveVersion={target_cv} (adopted)")
            except Exception as exc:
                out(f"  [!] Could not read template save version: {exc}")
    if target_cv is None:
        # saveVersion describes the chunk data, and conversion does not change
        # the chunk data - it only repacks the container. So carry the source's
        # version across. Writing a different number would describe the world
        # incorrectly and leave the game to parse it the wrong way.
        target_cv = cv or (DEFAULT_PS3_SAVE_VERSION if platform == 'ps3'
                           else DEFAULT_XBOX_SAVE_VERSION)

    # A save can never be at a version older than the build that created it.
    # ov > cv is a state no real save is ever in, and the game's upgrade path
    # is not written to survive it.
    if target_cv < ov:
        out(f"  [!] saveVersion {target_cv} would be older than "
            f"originalVersion {ov}, which no real save ever is. Using {ov}.")
        target_cv = ov

    out(f"  Writing saveVersion {target_cv} (original {ov})")

    for w in check_compatibility(ov, cv, target_cv, platform):
        out(f"  [!] {w}")
        if warnings is not None:
            warnings.append(w)

    label = 'PS3' if platform == 'ps3' else 'Xbox 360'
    out(f"Converting files  Windows LCE (LE) -> {label} (BE) ...")

    kept_entries, blobs = [], []
    dropped_regions = cropped_chunks = 0

    for e in entries:
        fn = e['filename']
        s, l = e['start_offset'], e['length']
        if s + l > len(payload):
            raise RuntimeError(f"Entry {fn!r} runs past the end of the payload.")
        blob = payload[s: s + l]
        parsed = parse_region_name(fn)

        if parsed:
            dim, rx, rz = parsed
            # Regions beyond the console's world simply do not exist there.
            if not region_intersects_console(dim, rx, rz):
                out(f"  Cropped : {fn:<28} outside the console world")
                dropped_regions += 1
                continue
            if blob:
                before = len(blob)
                counter = [0]
                blob = convert_region_to_console(
                    blob, platform,
                    log=lambda m, n=fn: out(f"    [!] {n}: {m}"),
                    verify=verify, name=fn, region=(rx, rz),
                    bounds=console_bounds_for(dim), dropped=counter)
                cropped_chunks += counter[0]
                extra = f"  (-{counter[0]} chunks)" if counter[0] else ""
                out(f"  Region  : {fn:<28} {before:>9,} -> "
                    f"{len(blob):>9,}{extra}")
            else:
                out(f"  Region  : {fn:<28} {len(blob):>9,}")
        else:
            if fn.lower() == 'level.dat' and blob:
                blob = _fit_level_dat_to_console(blob, out)
            elif fn.lower().startswith('players/') and blob:
                blob = _fit_player_to_console(blob, fn, out)
            out(f"  File    : {fn:<28} {len(blob):>9,}")

        kept_entries.append(e)
        blobs.append(blob)

    if dropped_regions or cropped_chunks:
        out(f"  Cropped to the console world: {dropped_regions} region files "
            f"removed, {cropped_chunks:,} chunks outside the border dropped")

    out("Building big-endian payload ...")
    be_payload = build_payload(ov, target_cv, kept_entries, blobs, endian='>')
    out(f"  BE payload   : {len(be_payload):,} bytes")

    for w in check_size(len(be_payload)):
        out(f"  [!] {w}")
        if warnings is not None:
            warnings.append(w)

    thumb = _find_win64_thumbnail(ms_path)

    if platform == 'ps3':
        return _write_ps3_save(be_payload, name, thumb, out_path, out)

    out("Compressing savegame.dat (XMemCompress / LZX) ...")
    savegame_dat = build_savegame_dat(be_payload, verify=verify)
    out(f"  savegame.dat : {len(savegame_dat):,} bytes")
    if verify:
        out("  LZX self-check: passed")

    if emulator:
        dst = Path(out_path) if out_path else OUTPUT_DIR / name
        dst.mkdir(parents=True, exist_ok=True)
        (dst / EMU_DAT_NAME).write_bytes(savegame_dat)
        out(f"  {EMU_DAT_NAME}  [ok]")
        (dst / EMU_THUMB_NAME).write_bytes(thumb or _placeholder_png())
        out(f"  {EMU_THUMB_NAME}  [ok]")
        # The folder alone is not enough - _MinecraftSaveInfo is what the
        # game reads to know a world is there, and what it is called.
        try:
            register_world(dst.parent, dst.name, name,
                           thumb or _placeholder_png())
            out(f"  {SAVEINFO_NAME}  [ok]  ({name})")
        except Exception as exc:
            out(f"  {SAVEINFO_NAME}  [skipped: {type(exc).__name__}]")
        out()
        out(f"Done!  ->  {dst}")
        out("Drop this folder into your emulator's Minecraft save directory.")
        return str(dst)

    dst = Path(out_path) if out_path else OUTPUT_DIR / f"{name}.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)
    out("Building STFS CON package ...")
    out(f"  Header       : "
        f"{Path(template_bin).name if template_raw else XBOX_HEADER_TPL.name}")

    # Writing into Content\<ProfileId>\... says whose save this is, so take it
    # from the destination rather than leaving the template's owner on it.
    if not profile_id:
        profile_id = profile_id_from_path(dst)
        from_path = bool(profile_id)
    else:
        from_path = False

    con = build_stfs_con(savegame_dat, template_raw, display_name=name,
                         thumbnail=thumb or _placeholder_png(),
                         profile_id=profile_id)
    dst.write_bytes(con)
    out(f"  World name   : {name}")
    out(f"  Thumbnail    : {'copied from the world' if thumb else 'placeholder'}")
    out(f"  Package      : {len(con):,} bytes")
    out(f"  Profile ID   : {read_profile_id(con)}"
        f"{'  (from the folder you are saving into)' if from_path else ''}")
    out(f"  Signature    : {'console-signed' if not check_con_signature(con) else 'FAILED'}")
    out()
    out(f"Done!  ->  {dst}")
    out("Next: copy it to your Xbox 360 storage device. It is already rehashed")
    out("      and resigned, so it does not need a trip through Horizon.")
    out("If the console does not list the world, the Profile ID above is not")
    out("the gamertag you are signed in as - set it on the convert page.")
    return str(dst)


def _write_ps3_save(be_payload, name, thumb, out_path, out):
    """PS3 saves are a folder: GAMEDATA (uncompressed) + PARAM.SFO + THUMB."""
    dir_name = f"{PS3_TITLE_ID}--{sanitise(name).upper().replace(' ', '')}"[:63]
    dst = Path(out_path) if out_path else OUTPUT_DIR / dir_name
    dst.mkdir(parents=True, exist_ok=True)

    (dst / 'GAMEDATA').write_bytes(be_payload)
    out(f"  GAMEDATA     : {len(be_payload):,} bytes (uncompressed)")
    # SAVEDATA_DIRECTORY has to match the folder actually on disk - the PS3
    # identifies the save by it, and the caller may have chosen the path.
    (dst / 'PARAM.SFO').write_bytes(build_param_sfo(name, dst.name))
    out("  PARAM.SFO    [ok]")
    (dst / 'THUMB').write_bytes(thumb or _placeholder_png())
    out("  THUMB        [ok]")

    out()
    out(f"Done!  ->  {dst}")
    out("[!] PS3 output is experimental and has not been tested on hardware.")
    out("[!] A real PS3 also needs a valid PARAM.PFD, which this tool cannot")
    out("    generate. Resign the folder with a PS3 save resigner first.")
    out("    RPCS3 is generally more forgiving.")
    return str(dst)


def _nbt_int(blob: bytes, name: str):
    """Offset of a TAG_Int's payload, or None. NBT here is always big-endian."""
    sig = bytes([3, 0, len(name)]) + name.encode('ascii')
    i = blob.find(sig)
    return None if i < 0 else i + len(sig)


def _nbt_data_payload_start(blob: bytes):
    """
    Offset where the 'Data' compound's members begin.

    level.dat is TAG_Compound("") { TAG_Compound("Data") { ... } }, so this is
    the front of the member list. Compound members are unordered, which is what
    makes inserting a tag there legal.
    """
    if len(blob) < 10 or blob[0] != 10:
        return None
    p = 3 + struct.unpack_from('>H', blob, 1)[0]      # past the root's name
    if p + 3 > len(blob) or blob[p] != 10:
        return None
    return p + 3 + struct.unpack_from('>H', blob, p + 1)[0]


def _set_nbt_int(blob: bytes, name: str, value: int) -> bytes:
    """Set a TAG_Int, inserting it into the Data compound if it is not there."""
    off = _nbt_int(blob, name)
    if off is not None:
        buf = bytearray(blob)
        struct.pack_into('>i', buf, off, value)
        return bytes(buf)

    start = _nbt_data_payload_start(blob)
    if start is None:
        return blob
    tag = bytes([3, 0, len(name)]) + name.encode('ascii') + struct.pack('>i', value)
    return blob[:start] + tag + blob[start:]


def _console_block_range(dim: str = ''):
    lo, hi = console_bounds_for(dim)
    return lo * BLOCKS_PER_CHUNK, hi * BLOCKS_PER_CHUNK + BLOCKS_PER_CHUNK - 1


def detect_world_size(payload: bytes, endian: str, platform: str = None) -> dict:
    """
    What size this world is.

    level.dat's XZSize is authoritative when present. Saves that predate the
    tag have no size recorded at all - but they also predate world sizes, so
    they are Classic by definition, whatever fraction of it has been explored.
    """
    _, _, _, _, entries = parse_payload(payload, endian)

    level = b''
    lo = hi = None
    for e in entries:
        blob = payload[e['start_offset']: e['start_offset'] + e['length']]
        if e['filename'].lower() == 'level.dat':
            level = blob
            continue
        parsed = parse_region_name(e['filename'])
        if not parsed or parsed[0] or not blob:
            continue                            # overworld regions only
        _, rx, rz = parsed
        for slot in range(REGION_SECT_COUNT):
            v = struct.unpack_from(endian + 'I', blob, slot * 4)[0]
            if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
                continue
            cx = rx * CHUNKS_PER_REGION + slot % CHUNKS_PER_REGION
            cz = rz * CHUNKS_PER_REGION + slot // CHUNKS_PER_REGION
            lo = min(cx, cz) if lo is None else min(lo, cx, cz)
            hi = max(cx, cz) if hi is None else max(hi, cx, cz)

    explored = 0 if lo is None else hi - lo + 1
    off = _nbt_int(level, 'XZSize') if level else None
    if off is not None:
        chunks = struct.unpack_from('>i', level, off)[0]
        source = 'level.dat'
    else:
        # No XZSize means a save from before world sizes existed, which is
        # always Classic. How much of it has been explored says nothing about
        # how big it is - a brand new world is Classic too.
        classic = WORLD_SIZES['classic'][1]
        if explored <= classic:
            return {'key': 'classic', 'label': 'Classic', 'chunks': classic,
                    'blocks': classic * BLOCKS_PER_CHUNK,
                    'explored_chunks': explored,
                    'source': f"no size tag, so Classic; "
                              f"{explored} chunks generated so far"}
        chunks = explored
        source = 'measured'

    for key, (label, size, _hs) in WORLD_SIZES.items():
        if chunks == size:
            return {'key': key, 'label': label, 'chunks': size,
                    'blocks': size * BLOCKS_PER_CHUNK,
                    'explored_chunks': explored, 'source': source}

    # A world is one of the four, never in between, so snap up to the smallest
    # size that contains what is there.
    for key in ('classic', 'small', 'medium', 'large'):
        size = WORLD_SIZES[key][1]
        if chunks <= size:
            return {'key': key, 'label': WORLD_SIZES[key][0], 'chunks': size,
                    'blocks': size * BLOCKS_PER_CHUNK,
                    'explored_chunks': explored, 'source': source}
    return {'key': None, 'label': 'Unknown', 'chunks': chunks,
            'blocks': chunks * BLOCKS_PER_CHUNK,
            'explored_chunks': explored, 'source': source}


def describe_world_size(src, direction: str, platform: str) -> str:
    """One-line world size for the interface. Never raises."""
    try:
        if direction == 'to_win64':
            payload, _, _ = read_console_input(src, platform)
            info = detect_world_size(payload, '>')
        else:
            info = detect_world_size(read_savedata_ms(find_win64_save(src)), '<')
    except Exception as exc:
        return f"could not read world size ({type(exc).__name__})"
    if not info['chunks']:
        return "world size unknown (no chunks found)"
    return (f"{info['label']} - {info['blocks']} x {info['blocks']} blocks "
            f"({info['chunks']} chunks)")


# What each saveVersion means, from ESaveVersions in FileHeader.h.
SAVE_VERSION_NOTES = {
    1: ("Pre-release", "before the Xbox 360 launch"),
    2: ("Launch", "the original Xbox 360 save format"),
    3: ("Post-launch", "after the first save-breaking change"),
    4: ("New End", "the End was introduced"),
    5: ("Moved stronghold", "stronghold generation changed"),
    6: ("Map data mapping size", "TU14 - TU16"),
    7: ("Xbox One map data", "Xbox One only; no Xbox 360 title update writes it"),
    8: ("Compressed chunk storage", "TU17 - TU18"),
    9: ("Xbox One / Windows LCE", "TU19, TU20 and Windows LCE"),
}

# saveVersion -> which title updates write it. Measured from a world generated
# on every title update from TU0 to TU20; none of this is inferred.
# Which title updates write each save version, in full. Derived from
# SAVE_VERSION_SHORT rather than repeated, because keeping two copies is how
# this went stale before: the short table was updated to TU75 while this one
# still stopped at TU20, so every save version 10 world reported itself as
# "unrecognised".
SAVE_VERSION_NEVER_WRITTEN = (4, 7)


def save_version_title_updates(cv: int) -> str:
    """The title updates a save version is consistent with, spelled out."""
    if cv in SAVE_VERSION_NEVER_WRITTEN:
        return "never written by any Xbox 360 title update"
    span = SAVE_VERSION_SHORT.get(cv)
    if not span:
        return "unrecognised save version"
    if cv == 9:
        return f"{span}, and Windows LCE / Xbox One CU7"
    return span


# The same thing said briefly, for a table cell rather than a sentence.
#
# 10 and 11 come from spot checks rather than a run of every update, so they
# name the update they were seen on instead of claiming a range. Measured:
# TU25 and TU31 both still write 9, TU50 writes 10, TU75 writes 11.
# Which Java Edition each title update was built from, for converting to Java.
#
# Read off the per-update pages on minecraft.wiki, which say "This version is
# based on Java Edition X" in so many words. (The old Fandom wiki refuses
# automated requests, so minecraft.wiki is the one to use.)
#
# Entries marked inferred=True are NOT stated on the wiki:
#   TU10  no parity given at all. TU9 and TU11 are both 1.1, so it is bracketed.
#   TU50  only an internal build number, 1.8.1323.0.
#   TU75  only an internal build number, 1.12.2178.0.
TITLE_UPDATE_JAVA = {
    0:  ("Beta 1.6.6", False), 1:  ("Beta 1.6.6", False),
    2:  ("Beta 1.6.6", False), 3:  ("Beta 1.7.3", False),
    4:  ("Beta 1.7.3", False), 5:  ("Beta 1.8.1", False),
    6:  ("Beta 1.8.1", False), 7:  ("1.0.0", False),
    8:  ("1.0.0", False),      9:  ("1.1", False),
    10: ("1.1", True),         11: ("1.1", False),
    12: ("1.2.4", False),      13: ("1.2.4", False),
    14: ("1.3.1", False),      15: ("1.3.1", False),
    16: ("1.3.1", False),      17: ("1.3.1", False),
    18: ("1.3.1", False),      19: ("1.6.4", False),
    20: ("1.6.4", False),      25: ("1.6.4", False),
    31: ("1.8", False),        36: ("1.8", False),
    50: ("1.8", True),         54: ("1.12", False),
    69: ("1.13", False),       75: ("1.12", True),
}

# The anchors above, as a sorted list, for filling the gaps between them.
_JAVA_ANCHORS = sorted(TITLE_UPDATE_JAVA)

# The chunk format change at TU12 is the Java 1.2 world-height change, not a
# block-id change. Measured from the flatland samples: a TU11 chunk stores
# Blocks as 32,768 bytes (16 x 16 x 128), a TU13 chunk as 65,536 (16 x 16 x
# 256). TU11 is Java 1.1 and TU12 is Java 1.2.4, which is exactly where Java
# went from McRegion to Anvil and from 128 to 256 blocks tall.
#
# So the Java region format a world converts into follows from its height:
JAVA_REGION_MCREGION = 'McRegion (.mcr)'   # Java 1.1 and earlier, 128 tall
JAVA_REGION_ANVIL    = 'Anvil (.mca)'      # Java 1.2 and later, 256 tall
WORLD_HEIGHT_BY_ERA  = {128: JAVA_REGION_MCREGION, 256: JAVA_REGION_ANVIL}


def java_version_for(title_update, interpolate: bool = True):
    """
    The Java Edition a title update was built from.

    Only some updates state it on the wiki. For the rest, the nearest stated
    update at or BELOW the one asked for is used: versions only ever move
    forward, so a title update is at least whatever the one before it was.
    That makes the answer a floor rather than a guess.

    Pass interpolate=False to get None instead for anything not stated.
    """
    hit = TITLE_UPDATE_JAVA.get(title_update)
    if hit:
        return hit[0]
    if not interpolate or title_update is None:
        return None
    below = [n for n in _JAVA_ANCHORS if n < title_update]
    return TITLE_UPDATE_JAVA[max(below)][0] if below else None


def java_version_is_exact(title_update) -> bool:
    """True when the wiki states this update's Java version outright."""
    hit = TITLE_UPDATE_JAVA.get(title_update)
    return bool(hit) and not hit[1]


# saveVersion -> the title updates that write it, said briefly enough for a
# table cell.
#
# Measured from a world generated on every title update TU0 - TU75 (TU40 is
# the only one missing), so every boundary is pinned by adjacent samples.
SAVE_VERSION_SHORT = {
    2: "TU0 - TU4", 3: "TU5 - TU8", 4: "never used", 5: "TU9 - TU13",
    6: "TU14 - TU16", 7: "Xbox One only", 8: "TU17 - TU18", 9: "TU19 - TU35",
    10: "TU36 - TU68", 11: "TU69 - TU75",
}


def save_version_short(cv: int) -> str:
    return SAVE_VERSION_SHORT.get(cv, "unrecognised")

# The chunk payload changed twice, and NOT in step with saveVersion - TU11 and
# TU13 both write saveVersion 5 but store chunks differently.
#
#   TU0  - TU11   Blocks 32 KB, Data/light as nibbles, no Biomes
#   TU12 - TU16   Blocks 64 KB as TWO 32 KB planes - low IDs then high IDs,
#                 the high plane all zero in a vanilla world - Data/light
#                 widened to a full byte each, Biomes added
#   TU17 - TU20   compressed tile storage, not NBT at all
#
# These are the formats a title update *creates*. A world keeps the format it
# was made in: opening a TU16 world on TU17 leaves its chunks as wide NBT and
# only bumps saveVersion, so a later game reads both. That is why converting
# between formats is never needed in the forward direction.
CHUNK_ERAS = [
    (0, 11, 'classic NBT chunks, one 32 KB block plane'),
    (12, 16, 'wide NBT chunks, two 32 KB block planes'),
    (17, 20, 'compressed tile storage, not NBT'),
]


def chunk_era_for_tu(tu: int):
    for lo, hi, label in CHUNK_ERAS:
        if lo <= tu <= hi:
            return label
    return None


_CHM_DLL = None


def _get_chm_lzx():
    """CHMLib's LZX decoder, used to read Xbox region chunks."""
    global _CHM_DLL
    if _CHM_DLL is not None:
        return _CHM_DLL
    path = _HERE / 'chm_lzx.dll'
    if not path.exists():
        raise FileNotFoundError(f"chm_lzx.dll not found at {path}")
    dll = ctypes.CDLL(str(path))
    dll.chm_lzx_init.restype = ctypes.c_void_p
    dll.chm_lzx_init.argtypes = [ctypes.c_int]
    dll.chm_lzx_decompress.restype = ctypes.c_int
    dll.chm_lzx_decompress.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    dll.chm_lzx_teardown.restype = None
    dll.chm_lzx_teardown.argtypes = [ctypes.c_void_p]
    _CHM_DLL = dll
    return dll


def _chunk_frames(blob: bytes):
    """[(uncompressed_size, compressed_bytes)] for an XMemCompress stream."""
    out, pos = [], 0
    while pos < len(blob):
        hi = blob[pos]
        if hi == 0xFF:
            if pos + 5 > len(blob):
                break
            dst = (blob[pos + 1] << 8) | blob[pos + 2]
            src = (blob[pos + 3] << 8) | blob[pos + 4]
            pos += 5
        else:
            if pos + 2 > len(blob):
                break
            src = (hi << 8) | blob[pos + 1]
            dst = LZX_BLOCK_SIZE
            pos += 2
        if src == 0 or pos + src > len(blob):
            break
        out.append((dst, blob[pos: pos + src]))
        pos += src
    return out


def decode_region_chunk(blob: bytes) -> bytes:
    """
    Decode an Xbox 360 region chunk, following every LZX frame.

    converter.py's own decoder reads the first frame and stops, so a chunk
    holding more than 32 KB of RLE'd data loses everything past it. The tail of
    a chunk is where TileEntities live, so the chests and signs in those chunks
    come out blank - always the same ones, because it depends only on how large
    the chunk is.

    The frames share one LZX window: decoding each with a fresh state fails
    outright, which is why they have to go through a single decoder in order.
    """
    frames = _chunk_frames(blob)
    if not frames:
        return b''
    dll = _get_chm_lzx()
    state = dll.chm_lzx_init(17)                 # window = 2^17 = 128 KiB
    if not state:
        raise RuntimeError("chm_lzx_init failed")
    try:
        out = bytearray()
        for dst, src in frames:
            sb = (ctypes.c_ubyte * len(src)).from_buffer_copy(src)
            db = (ctypes.c_ubyte * dst)()
            if dll.chm_lzx_decompress(state, sb, len(src), db, dst) != 0:
                raise RuntimeError(
                    f"chm_lzx_decompress failed {len(out)} bytes in")
            out.extend(bytes(db[:dst]))
        return bytes(out)
    finally:
        dll.chm_lzx_teardown(state)


def install_chunk_decoder():
    """
    Replace converter.py's single-frame chunk decoder with the multi-frame one.

    _convert_region is otherwise good, so patching just the decoder keeps the
    upstream region logic and fixes every caller at once.
    """
    import sys as _sys
    _sys.path.insert(0, str(_HERE))
    try:
        from . import converter
    except ImportError:
        return False
    original = converter._decompress_region_chunk

    def patched(blob):
        try:
            got = decode_region_chunk(blob)
            if got:
                return got
        except Exception:
            pass
        return original(blob)

    converter._decompress_region_chunk = patched
    return True


def rle_decode(src: bytes) -> bytes:
    """
    4J's RLE, from compression.cpp DecompressRLE.

    Chunk payloads are RLE'd and *then* platform-compressed, so decompressing
    alone leaves encoded bytes - reading those as NBT gives nonsense lengths.

        0-254   a literal byte
        255 n   n<3  -> n+1 literal 0xFF bytes
        255 n b n>=3 -> n+1 copies of b
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        b = src[i]
        i += 1
        if b != 255:
            out.append(b)
            continue
        if i >= n:
            break
        count = src[i]
        i += 1
        if count < 3:
            out.extend(b'\xff' * (count + 1))
        elif i < n:
            out.extend(bytes([src[i]]) * (count + 1))
            i += 1
    return bytes(out)


# Biome id -> name. From Universal Minecraft Editor's asset files, whose
# CONSOLE/TU60 table is byte-identical to its PC/1.12 one - LCE biome ids are
# Java's. Worlds before TU12 store no biomes at all; the game recomputes them
# from the seed every load, which is why they shift when the generator changes.
BIOMES_PATH = TEMPLATES_DIR / 'biomes.json'
_BIOME_NAMES = None


def biome_names() -> dict:
    global _BIOME_NAMES
    if _BIOME_NAMES is None:
        try:
            import json
            _BIOME_NAMES = {b['id']: b['name']
                            for b in json.loads(BIOMES_PATH.read_text('utf-8'))}
        except Exception:
            _BIOME_NAMES = {}
    return _BIOME_NAMES


def read_world_biomes(payload: bytes, endian: str) -> dict:
    """
    Which biomes a world stores, and how many chunks each covers.

    Empty for anything before TU12 - those worlds have no Biomes array, so
    there is genuinely nothing to read.
    """
    counts = {}
    _, _, _, _, entries = parse_payload(payload, endian)
    for e in entries:
        parsed = parse_region_name(e['filename'])
        if not parsed or parsed[0] or not e['length']:
            continue                                  # overworld only
        region = payload[e['start_offset']: e['start_offset'] + e['length']]
        for slot in range(REGION_SECT_COUNT):
            v = struct.unpack_from(endian + 'I', region, slot * 4)[0]
            if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
                continue
            fo = ((v >> 8) & 0xFFFFFF) * SECT
            if fo + 8 > len(region):
                continue
            craw, _ = struct.unpack_from(endian + 'II', region, fo)
            clen = craw & CHUNK_LEN_MASK
            if clen == 0 or fo + 8 + clen > len(region):
                continue
            try:
                if endian == '>':
                    from .converter import _decompress_region_chunk
                    nbt = _decompress_region_chunk(region[fo + 8: fo + 8 + clen])
                else:
                    nbt = zlib.decompress(region[fo + 8: fo + 8 + clen])
                if craw & CHUNK_FLAG_RLE:
                    nbt = rle_decode(nbt)
            except Exception:
                continue
            data = _nbt_byte_array(nbt, 'Biomes')
            if data:
                for b in set(data):
                    counts[b] = counts.get(b, 0) + 1
    return counts


def _nbt_byte_array(nbt: bytes, name: str):
    """Payload of a TAG_ByteArray inside the Level compound, or None."""
    sig = bytes([7, 0, len(name)]) + name.encode('ascii')
    i = nbt.find(sig)
    if i < 0:
        return None
    o = i + len(sig)
    if o + 4 > len(nbt):
        return None
    n = struct.unpack_from('>i', nbt, o)[0]
    if not 0 < n <= 0x10000 or o + 4 + n > len(nbt):
        return None
    return nbt[o + 4: o + 4 + n]


def first_chunk(payload: bytes, endian: str):
    """
    The first overworld chunk that decodes, or None.

    Both the format description and the Java converter need one real chunk to
    look at, and finding it is fiddly enough to be worth doing in one place.
    """
    _, _, _, _, entries = parse_payload(payload, endian)
    region = None
    for e in entries:
        parsed = parse_region_name(e['filename'])
        if parsed and not parsed[0] and e['length']:
            region = payload[e['start_offset']: e['start_offset'] + e['length']]
            break
    if not region:
        return None

    for slot in range(REGION_SECT_COUNT):
        v = struct.unpack_from(endian + 'I', region, slot * 4)[0]
        if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
            continue
        fo = ((v >> 8) & 0xFFFFFF) * SECT
        if fo + 8 > len(region):
            continue
        craw, _dlen = struct.unpack_from(endian + 'II', region, fo)
        clen = craw & CHUNK_LEN_MASK
        if clen == 0 or fo + 8 + clen > len(region):
            continue
        try:
            if endian == '>':
                from .converter import _decompress_region_chunk
                rle = _decompress_region_chunk(region[fo + 8: fo + 8 + clen])
            else:
                rle = zlib.decompress(region[fo + 8: fo + 8 + clen])
            if craw & CHUNK_FLAG_RLE:
                rle = rle_decode(rle)
        except Exception:
            continue
        if rle:
            return rle
    return None


def chunk_storage_version(payload: bytes, endian: str):
    """
    The version word a TU17+ chunk starts with, or None for an NBT chunk.

    This is the thing that decides whether a world can be converted to Java,
    and it is NOT the same as saveVersion: TU31 writes saveVersion 9 like TU19
    but chunk version 10, which is a different layout entirely.
    """
    rle = first_chunk(payload, endian)
    if not rle or len(rle) < 2:
        return None
    if rle[0] == 10:                      # TAG_Compound - a pre-TU17 NBT chunk
        return None
    return struct.unpack_from('>h', rle, 0)[0]


def world_height(payload: bytes, endian: str):
    """
    How tall the world is, 128 or 256, or None if no chunk can be read.

    Java raised the limit from 128 to 256 in 1.2, and LCE followed at TU12 -
    the first title update built on Java 1.2.4. Measured from a real chunk
    rather than assumed: a pre-TU12 chunk stores Blocks as 32,768 bytes and a
    TU12 one as 65,536.
    """
    ver = chunk_storage_version(payload, endian)
    if ver is not None:
        return 256                        # every tile storage format is 256
    raw = first_chunk(payload, endian)
    if not raw:
        return None
    i = raw.find(bytes([7, 0, 6]) + b'Blocks')
    if i < 0:
        return None
    n = struct.unpack_from('>I', raw, i + 9)[0]
    return 256 if n >= 65536 else 128


def profile_picture(root, kind: str, xuid: str = None):
    """
    An emulator profile's gamerpic, as PNG bytes, or None.

    Nexia and Xenia both keep it beside the account data as a plain tile_64
    or tile_32 PNG. The gamertag lives in the Account file next to it - see
    account_gamertag, which undoes the RC4 obfuscation.
    """
    root = Path(root)
    if not xuid:
        xuid = nexia_profile_xuid(root)
    if not xuid:
        return None
    base = root / ('content' if kind == 'xenia' else 'content')
    for tile in ('tile_64.png', 'tile_32.png'):
        for hit in base.rglob(tile):
            if xuid.upper() in str(hit).upper():
                try:
                    data = hit.read_bytes()
                except OSError:
                    continue
                if data[:8] == PNG_MAGIC:
                    return data
    return None


def java_support(payload: bytes, endian: str):
    """
    (ok, reason) for converting this world to Java Edition.

    Never raises - a world you cannot convert should still be readable on the
    world page, so a failure here becomes a reason string rather than an error.
    """
    try:
        from . import lce_java
        import sys
        return lce_java.check_convertible(payload, sys.modules[__name__], endian)
    except Exception as exc:
        return False, f"could not be checked ({type(exc).__name__})"


def detect_chunk_era(payload: bytes, endian: str) -> str:
    """
    Which chunk format this world actually stores, read from a real chunk.

    saveVersion cannot answer this - TU11 and TU13 both write 5 but store
    chunks differently - so the chunk itself is decoded and identified.
    """
    _, _, _, _, entries = parse_payload(payload, endian)
    region = None
    for e in entries:
        parsed = parse_region_name(e['filename'])
        if parsed and not parsed[0] and e['length']:
            region = payload[e['start_offset']: e['start_offset'] + e['length']]
            break
    if not region:
        return "no overworld chunks to identify"

    for slot in range(REGION_SECT_COUNT):
        v = struct.unpack_from(endian + 'I', region, slot * 4)[0]
        if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
            continue
        fo = ((v >> 8) & 0xFFFFFF) * SECT
        if fo + 8 > len(region):
            continue
        craw, _dlen = struct.unpack_from(endian + 'II', region, fo)
        clen = craw & CHUNK_LEN_MASK
        if clen == 0 or fo + 8 + clen > len(region):
            continue
        try:
            if endian == '>':
                from .converter import _decompress_region_chunk
                rle = _decompress_region_chunk(region[fo + 8: fo + 8 + clen])
            else:
                rle = zlib.decompress(region[fo + 8: fo + 8 + clen])
            if craw & CHUNK_FLAG_RLE:
                rle = rle_decode(rle)
        except Exception:
            continue
        # root compound, then the "Level" compound, then its first member
        o = 3 + 3 + 5
        if len(rle) < o + 3 or rle[0] != 10:
            return "compressed tile storage, TU17 and later"
        tag = rle[o]
        nlen = struct.unpack_from('>H', rle, o + 1)[0]
        name = rle[o + 3: o + 3 + nlen]
        if tag == 7 and name == b'Blocks':
            return "classic NBT chunks, one 32 KB block plane (TU0 - TU11)"
        if tag == 2 and name == b'TerrainPopulatedFlags':
            return "wide NBT chunks, two 32 KB block planes (TU12 - TU16)"
        return "compressed tile storage, not NBT (TU17 and later)"
    return "no readable chunks"

# Windows LCE shipped once, alongside Xbox One CU7, the same day as Xbox 360
# TU19. So a Windows LCE world is always that one version - there is no range.
WINDOWS_LCE_TITLE_UPDATE = "CU7 / TU19 (the only Windows LCE version)"


def _nbt_string(blob: bytes, name: str):
    """Value of a TAG_String, or None."""
    sig = bytes([8, 0, len(name)]) + name.encode('ascii')
    i = blob.find(sig)
    if i < 0:
        return None
    o = i + len(sig)
    if o + 2 > len(blob):
        return None
    n = struct.unpack_from('>H', blob, o)[0]
    try:
        return blob[o + 2: o + 2 + n].decode('utf-8', 'replace')
    except Exception:
        return None


def describe_world(payload: bytes, endian: str, platform: str = None) -> dict:
    """
    Everything worth showing about a world before converting it.

    Only reports what the save actually states. Where a value bounds the title
    update rather than naming it, it is described as a range.
    """
    fto, ne, ov, cv, entries = parse_payload(payload, endian)
    size = detect_world_size(payload, endian)

    level = b''
    dims = set()
    names = []
    for e in entries:
        names.append(e['filename'])
        if e['filename'].lower() == 'level.dat':
            level = payload[e['start_offset']: e['start_offset'] + e['length']]
        parsed = parse_region_name(e['filename'])
        if parsed:
            dims.add(parsed[0] or 'Overworld')

    def as_int(tag):
        off = _nbt_int(level, tag) if level else None
        return struct.unpack_from('>i', level, off)[0] if off is not None else None

    # Overworld, Nether, End - the order they appear in game, not alphabetical.
    DIM_ORDER = [('Overworld', 'Overworld'), ('DIM-1', 'Nether'), ('DIM1', 'End')]
    ordered_dims = [name for key, name in DIM_ORDER if key in dims]

    label, meaning = SAVE_VERSION_NOTES.get(
        cv, ("Unknown", "not a version this tool has seen"))

    return {
        'entries': ne,
        'original_version': ov,
        'save_version': cv,
        'version_label': label,
        'version_meaning': meaning,
        'title_update': _title_update_range(ov, cv, platform),
        'size_label': size['label'],
        'size_blocks': size['blocks'],
        'size_chunks': size['chunks'],
        'size_source': size['source'],
        'generator_name': _nbt_string(level, 'generatorName'),
        'generator_version': as_int('generatorVersion'),
        'generator_options': _nbt_string(level, 'generatorOptions'),
        'level_name': _nbt_string(level, 'LevelName'),
        'seed': _nbt_long(level, 'RandomSeed'),
        'spawn': (as_int('SpawnX'), as_int('SpawnY'), as_int('SpawnZ')),
        'hell_scale': as_int('HellScale'),
        'dimensions': ordered_dims,
        'chunk_format': detect_chunk_era(payload, endian),
        'world_height': world_height(payload, endian),
        'chunk_version': chunk_storage_version(payload, endian),
        'title_update_range': title_update_text(payload, endian),
        'java_support': java_support(payload, endian),
        'payload_bytes': len(payload),
        'filenames': names,
    }


def _nbt_long(blob: bytes, name: str):
    sig = bytes([4, 0, len(name)]) + name.encode('ascii')
    i = blob.find(sig)
    return None if i < 0 else struct.unpack_from('>q', blob, i + len(sig))[0]


def _title_update_range(ov: int, cv: int, platform: str = None) -> str:
    """
    The title updates a save version is consistent with.

    Windows LCE is a special case: it shipped once, so its version is exact.
    Everything else is either measured from sample worlds or bounded between
    two measured points - never read out of the save, which stores no TU.
    """
    if platform == 'windows_lce':
        return WINDOWS_LCE_TITLE_UPDATE
    return save_version_title_updates(cv)


def _expand_level_dat(blob: bytes, key: str, out) -> bytes:
    """Widen a world's border by rewriting the size tags in level.dat."""
    label, chunks, hell = WORLD_SIZES[key]
    blob = _set_nbt_int(blob, 'XZSize', chunks)
    blob = _set_nbt_int(blob, 'HellScale', hell)
    out(f"    expanded to {label}: XZSize={chunks} "
        f"({chunks * BLOCKS_PER_CHUNK} blocks), HellScale={hell}")
    return blob


def _fit_level_dat_to_console(blob: bytes, out) -> bytes:
    """
    Make level.dat describe a Classic-sized world and keep spawn inside it.

    A spawn point left outside the cropped border would drop the player into
    chunks that no longer exist.
    """
    buf = bytearray(blob)
    lo, hi = _console_block_range()
    inset_lo, inset_hi = lo + 8, hi - 8

    moved = []
    for axis in ('SpawnX', 'SpawnZ'):
        off = _nbt_int(buf, axis)
        if off is None:
            continue
        val = struct.unpack_from('>i', buf, off)[0]
        clamped = max(inset_lo, min(inset_hi, val))
        if clamped != val:
            struct.pack_into('>i', buf, off, clamped)
            moved.append(f"{axis} {val} -> {clamped}")
    if moved:
        out(f"    spawn was outside the console world: {', '.join(moved)}")

    for tag, value in (('XZSize', CONSOLE_XZSIZE),
                       ('HellScale', CONSOLE_HELLSCALE)):
        off = _nbt_int(buf, tag)
        if off is None:
            continue
        old = struct.unpack_from('>i', buf, off)[0]
        if old != value:
            struct.pack_into('>i', buf, off, value)
            out(f"    {tag} {old} -> {value}")

    return bytes(buf)


def _fit_player_to_console(blob: bytes, name: str, out) -> bytes:
    """Pull a player standing outside the cropped world back inside it."""
    sig = b'\x09\x00\x03Pos'
    i = blob.find(sig)
    if i < 0:
        return blob
    o = i + len(sig)
    if o + 5 + 24 > len(blob) or blob[o] != 6:
        return blob
    if struct.unpack_from('>i', blob, o + 1)[0] != 3:
        return blob

    vo = o + 5
    x, y, z = struct.unpack_from('>3d', blob, vo)
    lo, hi = _console_block_range()
    nx = max(lo + 8, min(hi - 8, x))
    nz = max(lo + 8, min(hi - 8, z))
    if nx == x and nz == z:
        return blob

    buf = bytearray(blob)
    struct.pack_into('>3d', buf, vo, nx, y, nz)
    out(f"    {name}: player was outside the console world, "
        f"({x:.0f},{z:.0f}) -> ({nx:.0f},{nz:.0f})")
    return bytes(buf)


def _find_win64_thumbnail(ms_path):
    return find_thumbnail(Path(ms_path).parent)


def _placeholder_png():
    """1x1 transparent PNG, for when the source world has no thumbnail."""
    return bytes.fromhex(
        '89504e470d0a1a0a'
        '0000000d49484452000000010000000108060000001f15c489'
        '0000000a49444154789c63000100000500010d0a2db4'
        '0000000049454e44ae426082')


def _extract_dat_from_template(template_raw: bytes) -> bytes:
    import sys
    pass  # vendored: sys.path hack removed
    from .converter import STFSPackage
    dat = STFSPackage(template_raw).extract_savegame_dat()
    if dat is None:
        raise RuntimeError("Template contains no savegame.dat")
    return dat


# ---------------------------------------------------------------------------
# Title update retargeting (console -> console)
#
# Chunk versions 8, 9, 10 and 11 are one layout - proved by parsing each under
# the version 9 reader and by 2,912 tile entities resolving correctly across
# TU0 - TU68. So moving a world between those title updates does NOT mean
# re-encoding anything: the chunk bodies are already right, and only two
# numbers have to change.
#
#   * the version word at the front of every chunk
#   * saveVersion in the payload header
#
# That is the whole difference between a TU19 world and a TU31 one as far as
# the file format is concerned, which is why the in-game upgrader has so
# little to do and why a world that "resets" usually just disagreed on these.
#
# What this deliberately does NOT do is substitute blocks. Going backwards,
# a world may contain blocks the older game has never heard of - a TU31 world
# here holds acacia logs and sunflowers that TU19 has no id for. Handling that
# needs a table of which block arrived in which title update, and inventing
# one by guesswork would silently delete real blocks. So retargeting is
# offered where it is exact, and refused where it is not.
# ---------------------------------------------------------------------------

RETARGET_CHUNK_VERSIONS = (8, 9, 10, 11)

# Which title updates write each chunk storage version. The inverse of
# chunk_version_for_title_update, and the reason a save version's range can
# usually be cut down: the two disagree about where the boundaries fall, so
# each one narrows the other.
CHUNK_VERSION_TITLE_UPDATES = {
    8:  (17, 18),
    9:  (19, 30),
    10: (31, 59),
    11: (60, 68),
    12: (69, 75),
}


def chunk_version_for_title_update(tu: int):
    """The chunk version a title update writes, or None outside TU17 - TU68."""
    if tu is None:
        return None
    if 17 <= tu <= 18:
        return 8
    if 19 <= tu <= 30:
        return 9
    if 31 <= tu <= 59:
        return 10
    if 60 <= tu <= 68:
        return 11
    return None


def save_version_for_title_update(tu: int):
    """The saveVersion a title update writes, or None if not known."""
    for cv, span in SAVE_VERSION_SHORT.items():
        hits = [int(n) for n in re.findall(r'TU(\d+)', span)]
        if len(hits) == 2 and hits[0] <= tu <= hits[1]:
            return cv
        if len(hits) == 1 and hits[0] == tu:
            return cv
    return None


# The data/*.dat files are structure caches - where the game found a mineshaft,
# a village, an ocean monument. Most are old and belong in any world. A handful
# are for structures a newer update introduced, and carrying one into an older
# title update points that update at a structure it does not know. The game
# regenerates a missing cache, so dropping these on a downgrade is safe and
# tidier. First TUs are the structure's own introduction, not when a file
# happened to appear in a sample.
STRUCTURE_FILE_FIRST_TU = {
    'Monument': 31,        # Ocean Monument
    'EndCity': 46,         # End Cities
    'Mansion': 53,         # Woodland Mansion
    'Ocean Ruin': 69,      # Update Aquatic
    'Shipwreck': 69,
    'Buried Treasure': 69,
}


# World features newer than the earliest saves. Carrying them down crashes a
# game that has no code for them - a whole dimension it cannot register, or a
# chunk tag its parser does not expect. Every threshold below is read straight
# off genuine samples, not release notes: TU6 has no End and no TileTicks, TU7
# has both; TU17 still writes the small map file, TU26 the large one.
END_FIRST_TU = 7            # The End dimension (DIM1 region files) - TU7+
TILE_TICKS_FIRST_TU = 7     # scheduled-tick chunk tag - TU7+; TU0-6 have none
LARGE_MAP_FIRST_TU = 19     # data/largeMapDataMappings.dat (cv9+); older worlds
#                             use the small data/mapDataMappings.dat instead
VILLAGES_FIRST_TU = 15      # data/villages.dat - not in genuine TU0-14 saves


def _obsolete_world_file(filename: str, target_tu: int,
                         has_large_map: bool = False) -> bool:
    """True for a file whose feature the target title update predates.

    *has_large_map* says the source stores maps in the TU25+ large format; its
    map_N.dat files are then incompatible with a small-format target and go
    with the mappings file rather than orphaning a map the game cannot index.
    """
    name = filename.rstrip('\x00')
    low = name.lower()
    if target_tu is None:
        return False
    # The End arrived in TU7; before it the game has no End dimension to load.
    # End regions are DIM1/r.*.mcr; the Nether is DIM-1r.*.mcr (kept).
    if name.startswith('DIM1/') and target_tu < END_FIRST_TU:
        return True
    if low == 'data/largemapdatamappings.dat' and target_tu < LARGE_MAP_FIRST_TU:
        return True
    # A large-format map with its mappings dropped is an orphan the older game
    # crashes on at world select; drop the maps too.
    if has_large_map and target_tu < LARGE_MAP_FIRST_TU \
            and re.fullmatch(r'data/map_\d+\.dat', low):
        return True
    if low == 'data/villages.dat' and target_tu < VILLAGES_FIRST_TU:
        return True
    return False


def structure_file_belongs(filename: str, target_tu: int) -> bool:
    """
    False for a data/<Structure>.dat that postdates the target - drop it.

    Anything that is not a known newer-structure cache is kept: level.dat, the
    map data, the player files, and every structure old enough to exist in the
    target all pass.
    """
    name = filename.rstrip('\x00')
    if not name.lower().startswith('data/') or not name.lower().endswith('.dat'):
        return True
    stem = name[len('data/'):-len('.dat')]
    first = STRUCTURE_FILE_FIRST_TU.get(stem)
    return first is None or target_tu is None or target_tu >= first


def _drop_obsolete_structures(entries: list, blobs: list, target_tu: int):
    """
    (entries, blobs, [dropped names]) with newer-than-target structure caches
    removed. entries and blobs stay parallel for build_payload.
    """
    kept_e, kept_b, dropped = [], [], []
    for e, b in zip(entries, blobs):
        if structure_file_belongs(e['filename'], target_tu):
            kept_e.append(e)
            kept_b.append(b)
        else:
            dropped.append(e['filename'].rstrip('\x00')[len('data/'):-4])
    return kept_e, kept_b, dropped


def overworld_version_for_title_update(tu: int):
    """
    The header's OVERWORLD version for a title update, or None if not known.

    The savegame header carries two version words: the save version and, right
    after it, the overworld version. They must AGREE with the target - leaving
    the overworld version at the source's value (11, for a TU75 world) while
    writing v11 chunks and save version 10 is what makes the game read a v12
    world, find v11 chunks, and lock the load button.

    Read from the genuine per-update samples: the overworld version equals the
    save version everywhere except the two oldest families - save version 2
    (TU0-4) and 3 (TU5-8) both carry overworld version 0.
    """
    cv = save_version_for_title_update(tu)
    if cv is None:
        return None
    return 0 if cv in (2, 3) else cv


# ---------------------------------------------------------------------------
# level.dat, and why a downgraded world regenerates without it
#
# Recoding the chunks is not enough. level.dat carries the world's metadata,
# and the game reads it FIRST - before a single chunk. If it names tags from a
# version newer than the one now loading the world, the game decides the save
# is from the future and regenerates the whole thing rather than load it. The
# giveaway is DataVersion: a genuine TU0 level.dat has 13 tags and no
# DataVersion; a TU75 one has 42 tags and DataVersion 922. Carrying the TU75
# level.dat into a TU0 world is what makes it regenerate.
#
# So level.dat is downgraded alongside the chunks: every tag introduced after
# the target title update is removed from the Data compound, leaving the world
# describing itself as one the target can load. The tags are removed by
# splicing their bytes out - everything kept stays byte-for-byte, and a
# compound has no length field to fix up, only a terminating TAG_End.
#
# Read out of the genuine per-update sample worlds. A tag not listed here is a
# TU0 base tag and is always kept.
# ---------------------------------------------------------------------------
LEVEL_DAT_TAG_FIRST_TU = {
    # First TU each is seen in, read per-title-update off genuine samples.
    'GameType': 5, 'MapFeatures': 5, 'generatorName': 5,
    'generatorVersion': 5, 'hasBeenInCreative': 5, 'newSeaLevel': 5,
    'spawnBonusChest': 5,
    'StrongholdX': 7, 'StrongholdY': 7, 'StrongholdZ': 7, 'hardcore': 7,
    'hasStronghold': 7,
    'StrongholdEndPortalX': 9, 'StrongholdEndPortalZ': 9,
    'hasStrongholdEndPortal': 9,
    'allowCommands': 14, 'initialized': 14,
    'HellScale': 17, 'XZSize': 17,
    'DayTime': 19, 'generatorOptions': 19,
    'Difficulty': 25, 'clearWeatherTime': 25,
    'DifficultyLocked': 31,
    'BiomeCentreXChunk': 36, 'BiomeCentreZChunk': 36, 'BiomeScale': 36,
    'DataVersion': 46, 'DimensionData': 46, 'ModernEnd': 46,
}

_NBT_FIXED = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}


def _nbt_skip_payload(raw, off, tag, end):
    """Byte offset just past one tag's payload. Endian is big - console NBT."""
    if tag in _NBT_FIXED:
        return off + _NBT_FIXED[tag]
    if tag == 7:                                   # TAG_Byte_Array
        n = struct.unpack_from('>i', raw, off)[0]
        return off + 4 + n
    if tag == 8:                                   # TAG_String
        n = struct.unpack_from('>H', raw, off)[0]
        return off + 2 + n
    if tag == 9:                                   # TAG_List
        item = raw[off]
        n = struct.unpack_from('>i', raw, off + 1)[0]
        off += 5
        for _ in range(n):
            off = _nbt_skip_payload(raw, off, item, end)
        return off
    if tag == 10:                                  # TAG_Compound
        while off < end:
            t = raw[off]
            off += 1
            if t == 0:
                return off
            nl = struct.unpack_from('>H', raw, off)[0]
            off += 2 + nl
            off = _nbt_skip_payload(raw, off, t, end)
        return off
    if tag == 11:                                  # TAG_Int_Array
        n = struct.unpack_from('>i', raw, off)[0]
        return off + 4 + 4 * n
    if tag == 12:                                  # TAG_Long_Array
        n = struct.unpack_from('>i', raw, off)[0]
        return off + 4 + 8 * n
    raise ValueError(f'unknown NBT tag {tag}')


# Player (players/*.dat) top-level tags that a newer update added. On a
# downgrade the target has never heard of them; a strict loader can choke, so
# they are cut out. TimeSinceRest is the Phantom timer - Update Aquatic, TU69.
# First title update each player.dat tag appears in, read straight off genuine
# saves - one player per save version, TU0 through TU75. A pre-TU5 base game
# reads a player at spawn and its parser is not built for the newer tags, so a
# downgrade has to strip everything past the target or the world crashes at
# "Initializing server". The TU0 player carries only the 15 base tags (Pos,
# Motion, Rotation, Health, Air, Inventory, Fire, FallDistance, OnGround,
# Dimension, HurtTime, DeathTime, AttackTime, Sleeping, SleepTimer).
PLAYER_TAG_FIRST_TU = {
    # First TU each is seen in, read per-title-update off genuine samples.
    # TU5: hunger and the first XP fields
    'foodExhaustionLevel': 5, 'foodLevel': 5, 'foodTickTimer': 5,
    'foodSaturationLevel': 5, 'GamePrivileges': 5, 'Xp': 5, 'XpLevel': 5,
    'XpTotal': 5,
    # TU7: XpP (replaced Xp) and flight abilities
    'XpP': 7, 'abilities': 7,
    # TU14: ender chest contents
    'EnderItems': 14,
    # TU19: attributes, absorption, UUID and friends
    'HealF': 19, 'AbsorptionAmount': 19, 'Invulnerable': 19,
    'PortalCooldown': 19, 'UUID': 19, 'Score': 19, 'Attributes': 19,
    'SelectedItemSlot': 19,
    # TU31: per-player spawn point, XP seed, hurt timestamp
    'HurtByTimestamp': 31, 'XpSeed': 31, 'SpawnForced': 31, 'SpawnX': 31,
    'SpawnY': 31, 'SpawnZ': 31,
    # TU39: the held-item stack
    'SelectedItem': 39,
    # TU46: data-fixer version, elytra flight
    'DataVersion': 46, 'FallFlying': 46,
    # TU47: glowing effect
    'Glowing': 47,
    # TU69: Update Aquatic
    'TimeSinceRest': 69,
}


def _strip_compound_tags(raw, comp_start, comp_end, first_tu_map, target_tu):
    """Byte spans of a compound's children whose first TU is past the target."""
    cut = []
    off = comp_start
    while off < comp_end:
        t = raw[off]
        if t == 0:
            break
        tag_at = off
        nlen = struct.unpack_from('>H', raw, off + 1)[0]
        name = raw[off + 3: off + 3 + nlen].decode('latin1')
        payload_at = off + 3 + nlen
        nxt = _nbt_skip_payload(raw, payload_at, t, comp_end)
        if first_tu_map.get(name, 0) > target_tu:
            cut.append((tag_at, nxt))
        off = nxt
    return cut


def _downgrade_side_file(filename, blob, target_tu):
    """Downgrade level.dat or a players/*.dat; anything else passes through."""
    name = filename.rstrip('\x00').lower()
    if name == 'level.dat':
        return downgrade_level_dat(blob, target_tu)
    if name.startswith('players/') and name.endswith('.dat'):
        return downgrade_player_dat(blob, target_tu)
    return blob


HEALTH_FLOAT_FIRST_TU = 46      # Health is a TAG_Short before TU46, TAG_Float after


def _health_float_to_short(raw: bytes) -> bytes:
    """Rewrite a TAG_Float 'Health' as a TAG_Short - old builds read it as one.

    TU46+ stores Health as a float; before that it is a short. Left as a float,
    an old game reads the first two bytes of the float as the health value -
    which for full health (0x41A00000) is 0x41A0, a huge number, and the player
    cannot be hurt. So round the float and write it back as a short.
    """
    off = 3 + struct.unpack_from('>H', raw, 1)[0]
    while off < len(raw):
        t = raw[off]
        if t == 0:
            break
        nlen = struct.unpack_from('>H', raw, off + 1)[0]
        name = raw[off + 1 + 2: off + 3 + nlen]
        pay = off + 3 + nlen
        if t == 5 and name == b'Health':            # TAG_Float
            value = struct.unpack_from('>f', raw, pay)[0]
            short = max(-32768, min(32767, int(round(value))))
            return (raw[:off] + bytes([2]) + raw[off + 1: pay]
                    + struct.pack('>h', short) + raw[pay + 4:])
        off = _nbt_skip_payload(raw, pay, t, len(raw))
    return raw


def downgrade_player_dat(raw: bytes, target_tu: int) -> bytes:
    """
    Bring a players/*.dat down to the target: strip tags newer than it, and
    fix tags whose TYPE changed with the era.

    The world loads the player at spawn, so a newer tag - or a tag of the wrong
    type, like a float Health an old build reads as a short - is read the moment
    you press Load. Returns the input unchanged if unparseable.
    """
    try:
        if not raw or raw[0] != 10:
            return raw
        nlen = struct.unpack_from('>H', raw, 1)[0]
        root = 3 + nlen                             # first child of root
        cut = _strip_compound_tags(raw, root, len(raw),
                                   PLAYER_TAG_FIRST_TU, target_tu)
        out = bytearray(raw)
        for start, stop in reversed(cut):
            del out[start:stop]
        result = bytes(out)
        if target_tu is not None and target_tu < HEALTH_FLOAT_FIRST_TU:
            result = _health_float_to_short(result)
        return result
    except Exception:
        return raw


def _downgrade_generator_options(raw, gen_start, gen_end):
    """
    A v12 (TU69+) generatorOptions byte array, rewritten in the pre-TU69 shape.

    The field is a byte array with its OWN 4-byte length inside the NBT one.
    TU69+ pads that inner buffer to 1024 and writes a 0x05 form byte; the older
    format is tight (inner length ~32) with a 0xFF form byte and one fewer byte
    after the preset name. So: drop the padding, flip the form byte, and remove
    the inserted byte. Only touched when the v12 shape is recognised; anything
    else is left exactly as it was.

    Returns (new_bytes_for_the_field_including_its_nbt_length, changed).
    """
    # gen_start points at the NBT byte-array length (4 bytes), then the body.
    # The body is: form(1) + 0x00 + nameLen(1) + name + config + zero padding.
    # There is no separate inner length word - the NBT length covers it all.
    nbt_len = struct.unpack_from('>i', raw, gen_start)[0]
    body = raw[gen_start + 4: gen_start + 4 + nbt_len]
    if len(body) < 4:
        return None, False
    form = body[0]
    # Recognise the v12 form: 0x05 form byte and the buffer padded out big.
    if form != 0x05 or nbt_len < 256 or body[1] != 0x00:
        return None, False
    name_len = body[2]
    name = body[3: 3 + name_len]
    # Strip trailing zero padding to get the real config bytes.
    cfg = body[3 + name_len:].rstrip(b'\x00')
    # Remove the inserted 0x00: v12 config starts 01 00 ..., old starts 01 ...
    if len(cfg) >= 2 and cfg[0] == 0x01 and cfg[1] == 0x00:
        cfg = cfg[:1] + cfg[2:]
    # Flip the form byte to the old 0xFF; keep the 0x00 + name + config.
    rebuilt = b'\xff\x00' + bytes([name_len]) + name + cfg
    # The old buffer is padded to a small round size; genuine uses 0x20.
    pad_to = max(0x20, len(rebuilt))
    rebuilt = rebuilt.ljust(pad_to, b'\x00')
    return struct.pack('>i', len(rebuilt)) + rebuilt, True


def downgrade_level_dat(raw: bytes, target_tu: int) -> bytes:
    """
    Strip every level.dat tag newer than *target_tu* from the Data compound,
    and bring the generatorOptions byte array back to the pre-TU69 shape.

    Returns the input unchanged if it cannot be parsed - a level.dat we do not
    understand is safer left alone than half-rewritten. Big-endian NBT only,
    which is what the console stores; a Windows LCE (little-endian) world is
    not retargeted through this path.
    """
    try:
        end = len(raw)
        # Outer: TAG_Compound "" -> find the "Data" child compound.
        if not raw or raw[0] != 10:
            return raw
        off = 1
        nl = struct.unpack_from('>H', raw, off)[0]
        off += 2 + nl
        # Walk the outer compound's children to find Data.
        data_start = None
        while off < end:
            t = raw[off]
            if t == 0:
                break
            name_at = off + 1
            nlen = struct.unpack_from('>H', raw, name_at)[0]
            name = raw[name_at + 2: name_at + 2 + nlen].decode('latin1')
            payload_at = name_at + 2 + nlen
            nxt = _nbt_skip_payload(raw, payload_at, t, end)
            if name == 'Data' and t == 10:
                data_start = payload_at
                data_end = nxt
                break
            off = nxt
        if data_start is None:
            return raw

        # Inside Data: collect edits as (start, end, replacement). A newer tag
        # is cut whole (replacement b''); generatorOptions is rewritten in
        # place when it is the v12 padded shape and we are heading below TU69.
        edits = []
        off = data_start
        while off < data_end:
            t = raw[off]
            if t == 0:
                break
            tag_at = off
            name_at = off + 1
            nlen = struct.unpack_from('>H', raw, name_at)[0]
            name = raw[name_at + 2: name_at + 2 + nlen].decode('latin1')
            payload_at = name_at + 2 + nlen
            nxt = _nbt_skip_payload(raw, payload_at, t, data_end)
            if LEVEL_DAT_TAG_FIRST_TU.get(name, 0) > target_tu:
                edits.append((tag_at, nxt, b''))
            elif name == 'generatorOptions' and t == 7 and target_tu < 69:
                new_field, changed = _downgrade_generator_options(
                    raw, payload_at, nxt)
                if changed:
                    edits.append((payload_at, nxt, new_field))
            off = nxt

        if not edits:
            return raw
        out = bytearray(raw)
        # Back to front, so each edit's offsets still hold when it is applied.
        for start, stop, repl in sorted(edits, reverse=True):
            out[start:stop] = repl
        return bytes(out)
    except Exception:
        return raw


def retarget_payload(payload: bytes, target_tu: int, endian: str = '>',
                     verify: bool = True, log=None, substitute: int = 0,
                     translate: bool = True, cancel=None) -> bytes:
    """
    Rewrite a console payload so it reads as a different title update.

    Inside TU17 - TU68 this only changes the chunk version word and
    saveVersion; every chunk body is carried across untouched, so nothing can
    be lost. Crossing out of that family - a TU75 world down to TU19, or a
    TU19 world down to TU11 - re-encodes each chunk through lce_recode
    instead, which costs time but is still exact except where the older
    format has no room for a block id. Those are counted and reported.

    Going UP into TU69 - TU75 is not written yet; lce_recode says so.
    """
    def note(msg):
        if log:
            log(msg)

    want_save = save_version_for_title_update(target_tu)
    want_chunk = chunk_version_for_title_update(target_tu)
    have = chunk_storage_version(payload, endian)

    # Same layout on both sides AND nothing to substitute? Then a relabel is
    # exact and much faster. A relabel cannot swap blocks, so a target old
    # enough to be missing some has to take the slow path even when the
    # storage format is identical.
    import sys
    pass  # vendored: sys.path hack removed
    from . import lce_recode
    rules = lce_recode.substitutions_for(target_tu)
    if want_chunk is None or have not in RETARGET_CHUNK_VERSIONS             or rules['exact'] or rules['any']:
        return recode_payload(payload, target_tu, endian=endian,
                              verify=verify, log=log,
                              substitute=substitute, translate=translate,
                              cancel=cancel)

    fto, ne, ov, cv, entries = parse_payload(payload, endian)
    note(f"  Chunk version : {have} -> {want_chunk}")
    note(f"  Save version  : {cv} -> {want_save}")

    blobs, patched, total = [], 0, 0
    for e in entries:
        _check_cancel(cancel)
        blob = payload[e['start_offset']: e['start_offset'] + e['length']]
        if not parse_region_name(e['filename']) or len(blob) < SECT:
            # level.dat / players/*.dat are big-endian NBT on EVERY platform (only the
            # container + region storage are little-endian on Windows LCE), so downgrade
            # them regardless of the payload endian - else a WinLCE downgrade leaves the
            # old version stamps/tags in place and the world's TU floor stays high.
            blob = _downgrade_side_file(e['filename'], blob, target_tu)
            blobs.append(blob)
            continue
        out = bytearray(blob)
        for slot in range(REGION_SECT_COUNT):
            v = struct.unpack_from(endian + 'I', out, slot * 4)[0]
            if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
                continue
            fo = ((v >> 8) & 0xFFFFFF) * SECT
            if fo + 8 > len(out):
                continue
            craw, dlen = struct.unpack_from(endian + 'II', out, fo)
            clen = craw & CHUNK_LEN_MASK
            if clen == 0 or fo + 8 + clen > len(out):
                continue
            body = bytes(out[fo + 8: fo + 8 + clen])
            try:
                raw = decode_region_chunk(body) if endian == '>' \
                    else zlib.decompress(body)
                if craw & CHUNK_FLAG_RLE:
                    raw = rle_decode(raw)
            except Exception:
                continue
            if len(raw) < 2:
                continue
            total += 1
            if struct.unpack_from('>h', raw, 0)[0] == want_chunk:
                continue
            # Only the two version bytes move; the body is byte-identical.
            fixed = struct.pack('>h', want_chunk) + raw[2:]
            new_body = _recompress_chunk(fixed, bool(craw & CHUNK_FLAG_RLE),
                                         endian, verify)
            if new_body is None or len(new_body) > clen:
                # Never grow a chunk in place - writing a body larger than its
                # allocated span would overwrite the FOLLOWING chunk's sectors and
                # corrupt the region. Leave this one chunk byte-identical (it keeps
                # its source version word); a relabel must not cost bytes. This is
                # extremely rare since only 2 bytes changed before recompression.
                continue
            out[fo + 8: fo + 8 + len(new_body)] = new_body
            struct.pack_into(endian + 'I', out, fo,
                             (craw & ~CHUNK_LEN_MASK) | len(new_body))
            patched += 1
        blobs.append(bytes(out))

    note(f"  Chunks        : {patched:,} relabelled of {total:,}")
    entries, blobs, dropped = _drop_obsolete_structures(
        entries, blobs, target_tu)
    if dropped:
        note(f"  Structures    : dropped {', '.join(dropped)} - not in "
             f"TU{target_tu}")
    want_ov = overworld_version_for_title_update(target_tu)
    if want_ov is None:
        want_ov = ov
    return build_payload(want_ov, want_save, entries, blobs, endian=endian)


class ConversionCancelled(RuntimeError):
    """Raised when a caller's cancel() asks a conversion to stop early."""


def _check_cancel(cancel):
    """Raise if the caller wants out. cancel() is polled, never trusted None."""
    if cancel is not None and cancel():
        raise ConversionCancelled('cancelled')


def recode_payload(payload: bytes, target_tu: int, endian: str = '>',
                   verify: bool = True, log=None, substitute: int = 0,
                   translate: bool = True, cancel=None) -> bytes:
    """
    Rewrite every chunk in a payload into another title update's format.

    This is the slow path, taken when the source and target store chunks
    differently - which is every hop across TU16/TU17, TU68/TU69, and the
    128/256 height change at TU12. Each chunk is decoded to flat arrays and
    written back out in the target's layout.

    Blocks, block data, lighting, height map, biomes, entities and tile
    entities all survive. Two things do not, and both are reported rather
    than hidden: a block id over 255 has nowhere to go in a pre-TU69 format,
    and going to a 128-tall world drops everything above y127.
    """
    import sys
    pass  # vendored: sys.path hack removed
    from . import lce_recode

    def note(msg):
        if log:
            log(msg)

    target, tall = lce_recode.target_for_title_update(target_tu)
    want_save = save_version_for_title_update(target_tu)
    have = chunk_storage_version(payload, endian)
    fto, ne, ov, cv, entries = parse_payload(payload, endian)

    note(f"  Chunk format  : {have if have is not None else 'NBT'} -> "
         f"{target if target is not None else ('NBT 256' if tall else 'NBT 128')}")
    note(f"  Save version  : {cv} -> {want_save}")
    note("  Re-encoding every chunk - this is not a relabel, so it takes a "
         "while.")

    keep_tile_ticks = target_tu is None or target_tu >= TILE_TICKS_FIRST_TU
    has_large_map = any(e['filename'].rstrip('\x00').lower()
                        == 'data/largemapdatamappings.dat' for e in entries)
    blobs, done, failed, replaced, swapped, obsolete = [], 0, 0, 0, 0, []
    kept_entries = []
    for e in entries:
        _check_cancel(cancel)               # once per file - cheap, responsive
        # Drop whole-world features the target predates - the End dimension,
        # the large-format maps, villages - or the game crashes registering or
        # indexing something it has no code for. Reported, not hidden.
        if _obsolete_world_file(e['filename'], target_tu, has_large_map):
            obsolete.append(e['filename'].rstrip('\x00'))
            continue
        kept_entries.append(e)
        blob = payload[e['start_offset']: e['start_offset'] + e['length']]
        if not parse_region_name(e['filename']) or len(blob) < SECT:
            # level.dat and the player have to come down too, or the game reads
            # newer version stamps and tags than itself - and regenerates the
            # world or crashes loading the player. These side files are big-endian
            # NBT on every platform (incl. Windows LCE), so downgrade them whatever
            # the container endian is.
            before = len(blob)
            blob = _downgrade_side_file(e['filename'], blob, target_tu)
            if len(blob) != before:
                note(f"  {e['filename'].rstrip(chr(0)):<14}: trimmed to "
                     f"TU{target_tu} ({before} -> {len(blob)} bytes)")
            blobs.append(blob)
            continue

        # A region is rebuilt rather than patched in place: a re-encoded chunk
        # is a different size, so nothing can be written back where it was.
        chunks = {}
        for slot in range(REGION_SECT_COUNT):
            v = struct.unpack_from(endian + 'I', blob, slot * 4)[0]
            if v == 0 or ((v >> 8) & 0xFFFFFF) < 2:
                continue
            fo = ((v >> 8) & 0xFFFFFF) * SECT
            if fo + 8 > len(blob):
                continue
            craw, _dlen = struct.unpack_from(endian + 'II', blob, fo)
            clen = craw & CHUNK_LEN_MASK
            if clen == 0 or fo + 8 + clen > len(blob):
                continue
            try:
                raw = decode_region_chunk(blob[fo + 8: fo + 8 + clen]) \
                    if endian == '>' else zlib.decompress(blob[fo + 8: fo + 8 + clen])
                if craw & CHUNK_FLAG_RLE:
                    raw = rle_decode(raw)
                out, lost, changed = lce_recode.recode_chunk(
                    raw, target, tall=tall, substitute=substitute,
                    target_tu=target_tu, translate=translate,
                    tile_ticks=keep_tile_ticks)
            except Exception:
                failed += 1
                continue
            chunks[slot] = (out, bool(craw & CHUNK_FLAG_RLE))
            replaced += lost
            swapped += changed
            done += 1

        blobs.append(_rebuild_region(chunks, endian, verify))

    note(f"  Chunks        : {done:,} re-encoded"
         + (f", {failed:,} left alone" if failed else ""))
    if not keep_tile_ticks:
        note("  TileTicks     : stripped - pre-TU5 chunks carry no scheduled "
             "ticks")
    if obsolete:
        note(f"  Dropped       : {', '.join(obsolete)} - feature not in "
             f"TU{target_tu}")
    if swapped:
        note(f"  Blocks swapped: {swapped:,} became the nearest block this "
             f"title update has - granite to stone, and so on")
    if replaced:
        note(f"  Blocks lost   : {replaced:,} had an id this format cannot "
             f"store at all, and are now air")
    entries = kept_entries
    # Drop structure caches for structures the target is too old to have -
    # a woodland mansion cache in a TU19 world points it at something it does
    # not know. The game regenerates a missing one, so this only tidies up.
    entries, blobs, dropped = _drop_obsolete_structures(
        entries, blobs, target_tu)
    if dropped:
        note(f"  Structures    : dropped {', '.join(dropped)} - not in "
             f"TU{target_tu}")

    # The overworld version has to move to the target too, not keep the
    # source's - a mismatch here locks the world in the load menu.
    want_ov = overworld_version_for_title_update(target_tu)
    if want_ov is None:
        want_ov = ov
    if want_ov != ov:
        note(f"  Overworld ver : {ov} -> {want_ov}")
    return build_payload(want_ov, want_save, entries, blobs, endian=endian)


# A region begins with two 4 KB sectors: slot pointers, then timestamps. Chunk
# data therefore starts at sector 2, and a pointer below that means "no chunk".
REGION_HEADER_SECTORS = 2


def _rebuild_region(chunks: dict, endian: str, verify: bool) -> bytes:
    """
    A region file built from scratch out of re-encoded chunks.

    The header is TWO sectors - 4,096 bytes of slot pointers and 4,096 of
    timestamps - so chunk data starts at sector 2. That second sector is easy
    to miss and expensive to miss: a chunk written at sector 1 is skipped by
    every reader, including this one, because a pointer below 2 means "no
    chunk". It cost exactly one chunk per region, silently.

    Every chunk changes size, so this cannot patch in place - it rebuilds.
    """
    header = bytearray(REGION_HEADER_SECTORS * SECT)
    body = bytearray()
    for slot, (raw, use_rle) in sorted(chunks.items()):
        # The chunk header's second word is the FINAL decompressed size the
        # game allocates for - the length after RLE *decode* (len(raw)), not
        # the RLE-encoded size. Writing the encoded size makes the game size the
        # chunk buffer ~20x too small, then RLE-expand into it: the world hangs
        # at "Loading spawn area". So dlen is always len(raw), RLE or not.
        payload = rle_encode(raw) if use_rle else raw
        packed = _compress_chunk_payload(payload, endian, verify)
        if packed is None:
            continue
        sector = REGION_HEADER_SECTORS + len(body) // SECT
        entry = struct.pack(endian + 'II', len(packed)
                            | (CHUNK_FLAG_RLE if use_rle else 0), len(raw))
        body += entry + packed
        while len(body) % SECT:
            body.append(0)
        # High 24 bits are the sector, low 8 the length in sectors.
        span = (len(entry) + len(packed) + SECT - 1) // SECT
        struct.pack_into(endian + 'I', header, slot * 4,
                         (sector << 8) | min(span, 0xFF))
    return bytes(header) + bytes(body)


def rle_encode(src: bytes) -> bytes:
    """
    The inverse of rle_decode, matching 4J's DecompressRLE exactly.

        0-254      a literal byte
        255 n      n < 3   -> n+1 literal 0xFF bytes
        255 n b    n >= 3  -> n+1 copies of b

    So a run is only worth encoding from four bytes up, and 0xFF can never be
    a literal - it always has to go through the escape.
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        b = src[i]
        run = 1
        while run < 256 and i + run < n and src[i + run] == b:
            run += 1
        if run >= 4:
            out += bytes([255, run - 1, b])
            i += run
        elif b == 255:
            # 0xFF must be escaped even singly: 255 n emits n+1 of them.
            take = min(run, 3)
            out += bytes([255, take - 1])
            i += take
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _compress_chunk_payload(payload: bytes, endian: str, verify: bool):
    """Compress an already-RLE'd chunk body the way its platform stores it."""
    try:
        if endian == '>':
            return _compress_lzx_verified(payload, verify, 'chunk')
        return zlib.compress(payload, 9)
    except Exception:
        return None


def _recompress_chunk(raw: bytes, use_rle: bool, endian: str, verify: bool):
    """Re-encode one chunk body the way its platform stores them."""
    return _compress_chunk_payload(rle_encode(raw) if use_rle else raw,
                                   endian, verify)


RETARGET_TITLE_UPDATES = list(range(17, 69))

# Where a world can be moved. Every title update the game shipped: inside
# TU17 - TU68 that is a relabel, and crossing out of it re-encodes every chunk
# through lce_recode, which can now write all four storage formats.
RECODE_TITLE_UPDATES = list(range(0, 76))


def same_chunk_layout(have_version, have_height, target_tu: int) -> bool:
    """
    Would moving to *target_tu* leave every chunk body untouched?

    True means a relabel: the version word and saveVersion change and nothing
    else does. TU17 - TU68 all share one layout, so any hop inside that range
    qualifies. Below it, the two NBT shapes are only the same as each other
    at the same height - a 128-tall world and a 256-tall one are different
    formats wearing the same container.
    """
    want = chunk_version_for_title_update(target_tu)
    if want is not None:
        return have_version in RETARGET_CHUNK_VERSIONS
    if target_tu >= 69:
        return have_version == 12
    if have_version is not None:
        return False
    return (have_height or 256) == (256 if target_tu >= 12 else 128)

# Every title update the game ever shipped. Retargeting is exact only within
# RETARGET_TITLE_UPDATES, but the full list is what a title update dropdown
# should offer - an update missing from the list looks like an oversight,
# whereas one that is present and refused explains itself.
ALL_TITLE_UPDATES = list(range(0, 76))


def convert_console_to_console(src, platform='xbox360', target_tu=19,
                               out_path=None, log=None, world_name=None,
                               emulator=False, verify=True, profile_id=None,
                               translate=True, cancel=None):
    """
    Rewrite a console save as a different title update.

    Only the chunk version word and saveVersion change - the chunk bodies are
    carried across untouched - so this is exact within TU17 - TU68 and refused
    outside it. See retarget_payload for why.

    *translate* False deletes blocks the target is too old to have instead of
    replacing them with a nearest older match - a straight format conversion.
    """
    import sys
    pass  # vendored: sys.path hack removed
    install_chunk_decoder()

    def out(msg=''):
        (log or print)(msg)

    if platform == 'windows_lce':                 # little-endian saveData.ms source
        ms = find_win64_save(src)
        payload, endian = read_savedata_ms(ms), '<'
        name = read_worldname_txt(Path(ms).parent) or Path(ms).parent.name
        thumb = find_thumbnail(Path(ms).parent)
    else:
        payload, name, thumb = read_console_input(src, platform)
        endian = '>'
    if world_name:
        name = world_name
    name = (name or '').strip() or 'MinecraftSave'

    info = describe_world(payload, endian, platform=platform)
    out(f"  World         : {name}")
    out(f"  Now           : save version {info['save_version']}, "
        f"{info['chunk_format']}")
    out(f"Retargeting to TU{target_tu} ...")

    new_payload = retarget_payload(payload, target_tu, endian=endian,
                                   verify=verify, log=out, translate=translate,
                                   cancel=cancel)

    if platform == 'windows_lce':
        # A Windows-LCE TU change stays Windows LCE: write the little-endian payload
        # back as a native Win64 world folder (saveData.ms + thumbnail).
        dst = Path(out_path) if out_path else OUTPUT_DIR / sanitise(name)
        dst.mkdir(parents=True, exist_ok=True)
        (dst / 'saveData.ms').write_bytes(write_savedata_ms(new_payload))
        out("  saveData.ms  [ok]")
        thumb_path = dst / WIN64_THUMB_REL
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(thumb or _placeholder_png())
        out(f"  {WIN64_THUMB_REL.as_posix()}  [ok]")
    elif emulator:
        # The container the world lands in, then a clean, unique folder name
        # inside it - never the raw display name, which may carry spaces or
        # parentheses an older title update crashes on at world-select.
        savegame = build_savegame_dat(new_payload, verify=verify)
        container = Path(out_path).parent if out_path else OUTPUT_DIR
        container.mkdir(parents=True, exist_ok=True)
        dst = container / unique_world_folder(container, name)
        dst.mkdir(parents=True, exist_ok=True)
        thumb_png = downgrade_thumbnail(thumb or _placeholder_png())
        (dst / EMU_DAT_NAME).write_bytes(savegame)
        (dst / EMU_THUMB_NAME).write_bytes(thumb_png)
        # Same as the other emulator path: the folder alone is not enough.
        try:
            register_world(dst.parent, dst.name, name, thumb_png)
            out(f"  {SAVEINFO_NAME}  [ok]  ({name})")
        except Exception as exc:
            out(f"  {SAVEINFO_NAME}  [skipped: {type(exc).__name__}]")
    else:
        savegame = build_savegame_dat(new_payload, verify=verify)
        dst = Path(out_path) if out_path else OUTPUT_DIR / f"{sanitise(name)}.bin"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not profile_id:
            profile_id = profile_id_from_path(dst)
        con = build_stfs_con(savegame, None, display_name=name,
                             thumbnail=thumb or _placeholder_png(),
                             profile_id=profile_id)
        dst.write_bytes(con)
        out(f"  Profile ID    : {read_profile_id(con)}")
        out(f"  Signature     : "
            f"{'console-signed' if not check_con_signature(con) else 'FAILED'}")

    src_tu = history_title_update(src)
    if src_tu is None:
        src_tu = title_update_from_path(src)
    append_history(dst, describe_edition(platform, src_tu),
                   describe_edition(platform, target_tu), source_folder=src,
                   world_name=name)
    out()
    out(f"Done!  ->  {dst}")
    return str(dst)


# ---------------------------------------------------------------------------
# world_history.txt
#
# Written into every world folder this tool produces or touches. Facts at the
# top - what the world is called, what it started as, what it is now - and a
# numbered log underneath, one line per thing that happened to it.
#
# A save carries no record of where it came from. Once a world has been
# through a couple of conversions there is otherwise no way to know, not for
# the user and not for this tool when it reads the world back. Which is also
# why the log is append-only: it is a record, never a source of truth. The
# world's NAME always comes from the save, never from here.
# ---------------------------------------------------------------------------

HISTORY_NAME = 'world_history.txt'
# Worlds converted before the file was reorganised carry the old name.
LEGACY_HISTORY_NAMES = ('conversion-history.txt',)
HISTORY_HEADER = 'Original: '        # the old one-line format, still read

# The facts kept at the top of the file, in the order they are written.
# The order they are written in, and the only keys write_history will emit -
# a field missing from here is silently dropped however carefully it was set.
HISTORY_FIELDS = ('World name',
                  'Original title update', 'Original build', 'Original edition',
                  'Original created',
                  'Current title update', 'Current build', 'Current edition',
                  'Last saved')
HISTORY_LOG_HEADING = 'Log'


def build_engine_for(desc: str) -> str:
    """
    Which storage format an edition description implies.

    The title update is the thing that changes it, so the description already
    carries the answer - TU19 and TU30 are the same engine, TU30 and TU31 are
    not, and that boundary is what decides whether a world can be moved
    between them without re-encoding a single chunk.
    """
    if not desc:
        return ''
    if desc.startswith('Java'):
        return 'Java Anvil region files'
    if desc.startswith('Windows LCE'):
        return 'Windows LCE saveData.ms'
    m = re.search(r'TU(\d+)', desc)
    if not m:
        return ''
    tu = int(m.group(1))
    version = chunk_version_for_title_update(tu)
    if version is not None:
        low, high = CHUNK_VERSION_TITLE_UPDATES[version]
        return f'tile storage v{version} (TU{low} - TU{high})'
    if tu >= 69:
        return 'sectioned tile storage v12 (TU69 - TU75)'
    if tu >= 12:
        return 'NBT chunks, 256 tall (TU12 - TU16)'
    return 'NBT chunks, 128 tall (TU0 - TU11)'


def title_update_of(desc: str) -> str:
    """'TU19' out of an edition description, or '' - for the header block."""
    m = re.search(r'TU\d+', desc or '')
    return m.group(0) if m else ''


def describe_edition(platform: str, title_update=None, java_version=None):
    """'Xbox 360 TU19' / 'Java 1.6.4' / 'Windows LCE', for the history file."""
    label = {'xbox360': 'Xbox 360', 'ps3': 'PS3',
             'windows_lce': 'Windows LCE', 'java': 'Java'}.get(
                 platform, platform)
    if platform == 'java':
        return f"{label} {java_version}" if java_version else label
    if title_update is not None:
        return f"{label} TU{title_update}"
    return label


def history_path(folder):
    """Where a world's history is, preferring the current name."""
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    current = folder / HISTORY_NAME
    if current.is_file():
        return current
    for legacy in LEGACY_HISTORY_NAMES:
        if (folder / legacy).is_file():
            return folder / legacy
    return current


def read_history(folder):
    """
    ({facts}, [log lines]) from a world folder, or ({}, []) if it has none.

    Reads the older one-line "Original: ..." format too, so a world converted
    before the file was reorganised does not lose what it recorded.
    """
    try:
        text = history_path(folder).read_text('utf-8')
    except OSError:
        return {}, []

    info, lines, in_log = {}, [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip() == HISTORY_LOG_HEADING:
            in_log = True
            continue
        if line.startswith(HISTORY_HEADER):        # the old format's one line
            info['Original edition'] = line[len(HISTORY_HEADER):].strip()
            in_log = True
            continue
        if not in_log and ':' in line:
            key, value = line.split(':', 1)
            if key.strip() in HISTORY_FIELDS:
                info[key.strip()] = value.strip()
                continue
        lines.append(line)

    # Last saved is READ FROM THE SAVE, every time, not taken from the file.
    # The world is played between conversions, and the point of the field is
    # to say when it was last saved in game - a value frozen at the last
    # conversion would answer a different question. Original created is the
    # opposite: written once, because converting copies the save and a copy
    # carries the date it was copied, so the real one cannot be recovered.
    save = _save_file_in(folder)
    if save:
        live = format_world_date(world_dates(save).get('modified'))
        if live:
            info['Last saved'] = live
    return info, lines


def history_original(folder):
    """What a world started as, or None."""
    return read_history(folder)[0].get('Original edition')


def write_history(folder, info: dict, lines: list):
    """Write the facts block, then the log, in that order."""
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    width = max(len(f) for f in HISTORY_FIELDS)
    body = [f"{field:<{width}} : {info[field]}"
            for field in HISTORY_FIELDS if info.get(field)]
    body += ['', HISTORY_LOG_HEADING] + list(lines)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / HISTORY_NAME).write_text("\n".join(body) + "\n",
                                           encoding='utf-8')
        # One world, one history - do not leave the old file behind saying
        # something different.
        for legacy in LEGACY_HISTORY_NAMES:
            stale = folder / legacy
            if stale.is_file():
                stale.unlink()
    except OSError:
        pass


def carry_save_times(source, dest_folder):
    """
    Give the converted save the source's timestamps.

    A conversion is not a play session. Without this, every world converted
    today reads "last saved: just now", and the one thing the column is for -
    telling which worlds have actually been played recently - stops working
    the moment a batch of worlds is converted.

    Best effort: a source we cannot stat, or a destination we cannot touch,
    simply leaves the new file's own times in place.
    """
    source, dest_folder = Path(source), Path(dest_folder)
    src_file = _save_file_in(source) or (source if source.is_file() else None)
    dst_file = _save_file_in(dest_folder)
    if not src_file or not dst_file:
        return False
    try:
        st = src_file.stat()
        os.utime(dst_file, (st.st_atime, st.st_mtime))
    except OSError:
        return False
    return True


def _save_file_in(folder):
    """The save inside a world folder, whose dates are the world's dates."""
    folder = Path(folder)
    for name in (EMU_DAT_NAME, 'saveData.ms'):
        p = folder / name
        if p.is_file():
            return p
    return None


def append_history(folder, source_desc: str, target_desc: str,
                   source_folder=None, world_name: str = None):
    """
    Record one conversion, starting the file if this is the first.

    *source_folder* is where the world came from; if it already has a history
    the original line is carried across, so a world keeps its true starting
    point however many hops it has made.
    """
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    info, lines = read_history(folder)
    if not info.get('Original edition') and source_folder:
        carried, carried_lines = read_history(source_folder)
        if carried.get('Original edition'):
            info, lines = dict(carried), carried_lines
    if not info.get('Original edition'):
        info['Original edition'] = source_desc
        info['Original title update'] = title_update_of(source_desc)
        info['Original build'] = build_engine_for(source_desc)

    # When the world was FIRST made, recorded once and never rewritten. A
    # conversion writes a brand new file, which carries the date it was
    # written - so once this is lost it cannot be recovered, and the only
    # chance to write it down is while the source is still in front of us.
    if not info.get('Original created'):
        look = source_folder or folder
        made = world_dates(_save_file_in(look) or look).get('created')
        if made:
            info['Original created'] = format_world_date(made)

    # Converting is not playing. The new save was written seconds ago, but
    # "last saved" means the last time the world was saved IN GAME, so the
    # source's timestamps are carried onto the copy and the clock does not
    # restart every time a world is moved between title updates.
    if source_folder:
        carry_save_times(source_folder, folder)

    if world_name:
        info['World name'] = world_name
    info['Current edition'] = target_desc
    info['Current title update'] = title_update_of(target_desc)
    info['Current build'] = build_engine_for(target_desc)

    if world_name:
        lines.append(f'{len(lines) + 1}. "{world_name}"  {source_desc} -> '
                     f'{target_desc}')
    else:
        lines.append(f"{len(lines) + 1}. {source_desc} -> {target_desc}")
    write_history(folder, info, lines)


# ---------------------------------------------------------------------------
# Xbox 360 Account files
#
# The gamertag lives in an "Account" file next to the gamerpic, obfuscated
# rather than truly encrypted: RC4 with a key derived by HMAC-SHA1 from a
# fixed 16-byte console key. Ported from Horizon's XeKeysUnObfuscate.
#
#     [0x00] 16 bytes  HMAC of the body, and the seed the RC4 key comes from
#     [0x10] ...       RC4'd body: 8 bytes of confounder, then XamAccountInfo
#
# Verifying the HMAC after decrypting is what tells retail from devkit - the
# two use different keys, so the wrong one simply fails the check.
# ---------------------------------------------------------------------------

ACCOUNT_KEY_RETAIL = bytes.fromhex('e1bc159c73b1eae9ab3170f3ad47ebf3')
ACCOUNT_KEY_DEVKIT = bytes.fromhex('dab69ad98e28764f977ee2487e4f3f68')

# XamAccountInfo, from the start of the decrypted body:
#   0x00 u32 reserved, 0x04 u32 live flags, 0x08 wchar[16] gamertag.
ACCOUNT_GAMERTAG_OFF = 0x08
ACCOUNT_GAMERTAG_MAX = 16


def _rc4(data: bytes, key: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def deobfuscate_account(raw: bytes):
    """
    Decrypt an Account file. Returns the plain XamAccountInfo, or None.

    Tries the retail key first, then the devkit one; the embedded HMAC says
    which is right, so there is no guessing involved.
    """
    if len(raw) < 0x18:
        return None
    import hmac as _hmac
    seed, body = raw[:0x10], raw[0x10:]
    for key in (ACCOUNT_KEY_RETAIL, ACCOUNT_KEY_DEVKIT):
        rc4_key = _hmac.new(key, seed, hashlib.sha1).digest()[:0x10]
        plain = _rc4(body, rc4_key)
        if _hmac.new(key, plain, hashlib.sha1).digest()[:0x10] == seed:
            return plain[8:]                # drop the confounder
    return None


def account_gamertag(raw: bytes):
    """The gamertag from an Account file, or None if it will not decrypt."""
    info = deobfuscate_account(raw)
    if not info:
        return None
    blob = info[ACCOUNT_GAMERTAG_OFF:
                ACCOUNT_GAMERTAG_OFF + ACCOUNT_GAMERTAG_MAX * 2]
    name = blob.decode('utf-16-be', 'ignore').split('\x00')[0].strip()
    return name or None


def read_profile(root, xuid: str = None) -> dict:
    """
    {'xuid', 'gamertag', 'picture'} for an emulator profile.

    gamertag is None when the Account file is missing or will not decrypt,
    and picture is None when there is no gamerpic - neither is fatal, they
    are just not shown.
    """
    root = Path(root)
    if not xuid:
        xuid = nexia_profile_xuid(root)
    out = {'xuid': xuid, 'gamertag': None, 'picture': None}
    if not xuid:
        return out
    for account in _find_accounts(root):
        if xuid.upper() not in str(account).upper() or not account.is_file():
            continue
        try:
            out['gamertag'] = account_gamertag(account.read_bytes())
        except Exception:
            pass
        for tile in ('tile_64.png', 'tile_32.png'):
            pic = account.parent / tile
            if pic.is_file():
                data = pic.read_bytes()
                if data[:8] == PNG_MAGIC:
                    out['picture'] = data
                    break
        if out['gamertag'] or out['picture']:
            break
    return out


# ---------------------------------------------------------------------------
# _MinecraftSaveInfo
#
# How Nexia360 and the console itself know what a world is called. The folder
# name is NOT the world name - it only happens to match when nobody has
# renamed anything, which is exactly the sort of coincidence that hides a bug.
#
#     [0x00] u32 BE   number of records
#     then, per record, 0x134 bytes:
#         0x000  wchar[128] BE  world name
#         0x100  char[46]       the folder the world lives in
#         0x12E  u16 BE         where this thumbnail starts, counted from the
#                               first one - so it is a running total of every
#                               earlier length, not a file offset
#         0x130  u32 BE         length of this world's thumbnail
#     then every thumbnail back to back, in record order.
#
# Verified against a real Nexia library: the four length fields sum to 8,594
# bytes and the trailing data is exactly 8,594 bytes of PNG.
# ---------------------------------------------------------------------------

SAVEINFO_NAME = '_MinecraftSaveInfo'
SAVEINFO_RECORD = 0x134
SAVEINFO_UTF16_LEN = 0x100
SAVEINFO_FOLDER_OFF = 0x100
SAVEINFO_FOLDER_LEN = 0x2E
SAVEINFO_THUMB_AT = 0x12E
SAVEINFO_THUMB_OFF = 0x130


def saveinfo_path(content_root) -> Path:
    """Where the index lives, under a Content/<xuid>/<save id> folder."""
    return Path(content_root) / SAVEINFO_NAME / SAVEINFO_NAME


def read_saveinfo(content_root) -> list:
    """
    [{'name', 'folder', 'thumbnail'}] from a save id folder, or [].

    Never raises - a missing or malformed index just means we fall back to
    folder names, which is what happened before this existed.
    """
    f = saveinfo_path(content_root)
    try:
        raw = f.read_bytes()
    except OSError:
        return []
    if len(raw) < 4:
        return []
    try:
        count = struct.unpack_from('>I', raw, 0)[0]
    except struct.error:
        return []
    out, at = [], 4 + count * SAVEINFO_RECORD
    for i in range(count):
        off = 4 + i * SAVEINFO_RECORD
        rec = raw[off: off + SAVEINFO_RECORD]
        if len(rec) < SAVEINFO_RECORD:
            break
        name = rec[:SAVEINFO_UTF16_LEN].decode('utf-16-be', 'replace')
        name = name.split('\x00')[0].strip()
        folder = rec[SAVEINFO_FOLDER_OFF:
                     SAVEINFO_FOLDER_OFF + SAVEINFO_FOLDER_LEN]
        folder = folder.split(b'\x00')[0].decode('utf-8', 'replace')
        size = struct.unpack_from('>I', rec, SAVEINFO_THUMB_OFF)[0]
        thumb = raw[at: at + size]
        at += size
        out.append({'name': name, 'folder': folder,
                    'thumbnail': thumb if thumb[:8] == PNG_MAGIC else None})
    return out


def write_saveinfo(content_root, entries: list):
    """
    Write the index back. *entries* is [{'name', 'folder', 'thumbnail'}].

    Without this a converted world sits in the right folder and still does
    not appear, because nothing has told the game it exists.
    """
    head = struct.pack('>I', len(entries))
    records, blobs = bytearray(), bytearray()
    running = 0
    for e in entries:
        thumb = e.get('thumbnail') or b''
        rec = bytearray(SAVEINFO_RECORD)
        name = (e.get('name') or '')[:SAVEINFO_UTF16_LEN // 2 - 1]
        rec[:len(name) * 2] = name.encode('utf-16-be')
        folder = (e.get('folder') or '').encode('utf-8')[
            :SAVEINFO_FOLDER_LEN - 1]
        rec[SAVEINFO_FOLDER_OFF: SAVEINFO_FOLDER_OFF + len(folder)] = folder
        struct.pack_into('>H', rec, SAVEINFO_THUMB_AT, running & 0xFFFF)
        struct.pack_into('>I', rec, SAVEINFO_THUMB_OFF, len(thumb))
        running += len(thumb)
        records += rec
        blobs += thumb
    f = saveinfo_path(content_root)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(head + bytes(records) + bytes(blobs))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Headers
#
# The index says what a world is CALLED. A .header says the world is THERE.
# Nexia360 enumerates a profile's saves from Headers/, so a world with no
# header is on disk, correctly named, and invisible in the game - which is
# exactly how a converted world goes missing.
#
# The folder sits beside the save id, not inside it:
#     Content\<xuid>\00000001\<World>.bin\savegame.dat
#     Content\<xuid>\Headers\00000001\<World>.bin.header
#
# 328 bytes, read off a real Nexia library:
#     0x000  u32 BE          1
#     0x004  u32 BE          1
#     0x008  wchar[128] BE   display name, null terminated
#     0x108  char[56]        the folder name, matching the one on disk
#     0x140  u32 BE          title id
#     0x144  u32 BE          0
# ---------------------------------------------------------------------------

HEADERS_DIR = 'Headers'
SAVE_HEADER_SUFFIX = '.header'
SAVE_HEADER_SIZE = 0x148
SAVE_HEADER_NAME_OFF = 0x008
SAVE_HEADER_NAME_LEN = 0x100
SAVE_HEADER_FOLDER_OFF = 0x108
SAVE_HEADER_FOLDER_LEN = 0x38
SAVE_HEADER_TITLE_OFF = 0x140
SAVEINFO_DISPLAY_NAME = 'Minecraft Save Info'


def headers_dir(content_root) -> Path:
    """Headers/<save id>, the sibling of the save id folder."""
    content_root = Path(content_root)
    return content_root.parent / HEADERS_DIR / content_root.name


def build_save_header(folder_name: str, display_name: str,
                      title_id: int = None) -> bytes:
    """One 328-byte header record."""
    buf = bytearray(SAVE_HEADER_SIZE)
    struct.pack_into('>II', buf, 0, 1, 1)
    wide = display_name.encode('utf-16-be')[:SAVE_HEADER_NAME_LEN - 2]
    buf[SAVE_HEADER_NAME_OFF:SAVE_HEADER_NAME_OFF + len(wide)] = wide
    raw = folder_name.encode('utf-8')[:SAVE_HEADER_FOLDER_LEN - 1]
    buf[SAVE_HEADER_FOLDER_OFF:SAVE_HEADER_FOLDER_OFF + len(raw)] = raw
    if title_id is None:
        title_id = int(MINECRAFT_TITLE_ID_HEX, 16)
    struct.pack_into('>I', buf, SAVE_HEADER_TITLE_OFF, title_id)
    return bytes(buf)


def read_save_header_name(content_root, folder_name: str):
    """
    A world's display name out of its .header, or None.

    Worth trying because the index and the header are written independently:
    a world can have a perfectly good header and no _MinecraftSaveInfo beside
    it at all, which is what a NO_TU (base game) library looks like. Without
    this the world lists as the folder it sits in - "Save2026 8 71515 7.bin"
    instead of "New World".
    """
    p = headers_dir(content_root) / (folder_name + SAVE_HEADER_SUFFIX)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if len(raw) < SAVE_HEADER_SIZE:
        return None
    try:
        name = raw[SAVE_HEADER_NAME_OFF:
                   SAVE_HEADER_NAME_OFF + SAVE_HEADER_NAME_LEN]
        return name.decode('utf-16-be').split('\x00')[0].strip() or None
    except Exception:
        return None


def write_save_headers(content_root, folder_name: str, world_name: str,
                       with_index: bool = True):
    """
    The world's header, and - where there is an index - the index's own.

    Silently does nothing outside an emulator library: a plain output folder
    has no Headers level and inventing one there would be noise.
    """
    d = headers_dir(content_root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / (folder_name + SAVE_HEADER_SUFFIX)).write_bytes(
            build_save_header(folder_name, world_name))
        info = d / (SAVEINFO_NAME + SAVE_HEADER_SUFFIX)
        if with_index and not info.exists():
            info.write_bytes(build_save_header(SAVEINFO_NAME,
                                               SAVEINFO_DISPLAY_NAME))
    except OSError:
        pass
    return d


# ---------------------------------------------------------------------------
# Title updates
#
# Nexia keeps one folder per installed update, and TWO things that say the
# folder is real:
#
#     Library\584111F7\Title Update 19\UPDATE\...    the update's own files
#     Library\584111F7\Title Update 19.header        332 bytes, beside it
#     Library\584111F7\title_updates.json            the list the game reads
#
# A folder with none of that is not an installed update, and the installer
# treats the name as taken: install TU19 next to a bare "Title Update 19" and
# it creates "Title Update 19-2" instead, leaving any saves in the first one
# orphaned where the game will never look.
#
# The header is the same record as a save's, with two differences: the content
# type at 0x004 is TITLE UPDATE rather than SAVED GAME, and it is four bytes
# longer.
#
#     0x000  u32 BE   1
#     0x004  u32 BE   0x000B0000       content type - saves use 0x00000001
#     0x008  wchar[]  "Minecraft Title Update"   the same on every one
#     0x108  char[56] "Title Update 19"          the NAME, not the folder id
#     0x140  u32 BE   title id
#     0x144  u32 BE   0
#     0x148  u32 BE   0
#
# Note 0x108 on "Title Update 19-2.header" reads "Title Update 19" - the file
# name is the folder id and 0x108 is what it is called, exactly the id/name
# split title_updates.json uses.
# ---------------------------------------------------------------------------

TITLE_UPDATE_HEADER_SIZE = 0x14C
CONTENT_TYPE_SAVED_GAME = 0x00000001
CONTENT_TYPE_TITLE_UPDATE = 0x000B0000
TITLE_UPDATE_DISPLAY = 'Minecraft Title Update'
TITLE_UPDATES_JSON = 'title_updates.json'
UPDATE_DIR = 'UPDATE'
BASE_GAME_DIR = 'NO_TU'


def build_title_update_header(name: str, title_id: int = None) -> bytes:
    """One 332-byte title update header. *name* is "Title Update 19"."""
    buf = bytearray(TITLE_UPDATE_HEADER_SIZE)
    struct.pack_into('>I', buf, 0, 1)
    struct.pack_into('>I', buf, 4, CONTENT_TYPE_TITLE_UPDATE)
    wide = TITLE_UPDATE_DISPLAY.encode('utf-16-be')[:SAVE_HEADER_NAME_LEN - 2]
    buf[SAVE_HEADER_NAME_OFF:SAVE_HEADER_NAME_OFF + len(wide)] = wide
    raw = name.encode('utf-8')[:SAVE_HEADER_FOLDER_LEN - 1]
    buf[SAVE_HEADER_FOLDER_OFF:SAVE_HEADER_FOLDER_OFF + len(raw)] = raw
    if title_id is None:
        title_id = int(MINECRAFT_TITLE_ID_HEX, 16)
    struct.pack_into('>I', buf, SAVE_HEADER_TITLE_OFF, title_id)
    return bytes(buf)


def nexia_library(root) -> Path:
    """Library\\<title id>, where the title update folders live."""
    return Path(root) / 'Library' / MINECRAFT_TITLE_ID_HEX


def title_updates_path(root) -> Path:
    return nexia_library(root) / TITLE_UPDATES_JSON


def read_title_updates(root) -> dict:
    """
    title_updates.json, or an empty shape if it is missing or unreadable.

    Never raises: a library with no json yet is a normal state, and a broken
    one must not stop a conversion that would otherwise work.
    """
    try:
        data = json.loads(title_updates_path(root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'active': None, 'updates': []}
    if not isinstance(data, dict):
        return {'active': None, 'updates': []}
    data.setdefault('updates', [])
    data.setdefault('active', None)
    if not isinstance(data['updates'], list):
        data['updates'] = []
    return data


def registered_title_update_ids(root) -> set:
    """The folder ids title_updates.json lists, lowercased."""
    return {str(u.get('id', '')).lower()
            for u in read_title_updates(root)['updates'] if u.get('id')}


def title_update_is_installed(folder: Path, registered: set = None) -> bool:
    """
    Does this folder hold an actual update?

    Two independent signs, either of which is enough: the UPDATE folder with
    the game files in it, or an entry in title_updates.json. A folder with
    neither is a name someone created - our own converter does exactly that
    when it writes a world for an update that is not installed.
    """
    if (folder / UPDATE_DIR).is_dir():
        return True
    if registered is None:
        return False
    return folder.name.lower() in registered


class TitleUpdateNotInstalled(RuntimeError):
    """
    The library has no folder for the update a world is being written for.

    Not something to work around by creating the folder: Nexia will not
    install an update into a folder that already exists, so a folder made
    here means the real install lands beside it as "Title Update 19-2" and
    the world is left where the game never looks.
    """

    def __init__(self, title_update: int, root=None, doing: str = 'convert'):
        self.title_update = title_update
        self.root = root
        self.doing = doing
        super().__init__(
            f'Title Update {title_update} is not installed in this Nexia360 '
            f'folder.\n\nInstall Title Update {title_update} in Nexia360 '
            f'first, then {doing}. Creating the folder here would stop '
            f'Nexia360 installing the update into it later.')


def title_update_folder_name(title_update: int) -> str:
    """
    What the folder for an update is called.

    TU0 is not a title update at all - it is the game as it shipped, and the
    library calls that NO_TU.
    """
    return BASE_GAME_DIR if not title_update \
        else f'Title Update {title_update}'


def nexia_title_update_folder(root, title_update: int):
    """
    The folder a world for *title_update* belongs in, or None if there is none.

    Position and number are not enough to choose. A library can hold both
    "Title Update 19" and "Title Update 19-2" - the second is what the
    installer made when the first name was already taken - and they both parse
    as 19. Picking the first alphabetically picks the EMPTY one, and a world
    written there is invisible because the game boots the other.

    So: among the folders that claim this number, prefer one that is really
    installed, and among those the one with saves already in it.
    """
    lib = nexia_library(root)
    if not lib.is_dir():
        return None
    registered = registered_title_update_ids(root)
    hits = []
    for child in sorted(lib.iterdir()):
        if not child.is_dir():
            continue
        if title_update_from_path(child / 'Content' / 'x' / 'y') != title_update:
            continue
        hits.append(child)
    if not hits:
        return None
    return max(hits, key=lambda c: (title_update_is_installed(c, registered),
                                    (c / 'Content').is_dir()))


def title_update_installed(root, title_update: int) -> bool:
    """
    Is this update actually installed, or would we be inventing its folder?

    TU0 counts as installed when the base game is there - it needs no update.
    A world converted to an update that is NOT installed is written correctly
    and simply cannot be opened until the update is, which is worth saying out
    loud rather than leaving to be discovered.
    """
    folder = nexia_title_update_folder(root, title_update)
    if folder is None:
        return False
    if folder.name == BASE_GAME_DIR:
        return (folder / 'Content').is_dir()
    return title_update_is_installed(folder,
                                     registered_title_update_ids(root))


def ensure_title_update_header(root, folder_name: str, name: str = None):
    """
    The .header beside a title update folder, written only if missing.

    This is what stops the installer inventing "Title Update 19-2" later and
    stranding the worlds already written under "Title Update 19". An existing
    header is never rewritten - a real install's own header is the authority
    on its own name.
    """
    p = nexia_library(root) / (folder_name + SAVE_HEADER_SUFFIX)
    if p.exists():
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(build_title_update_header(name or folder_name))
    except OSError:
        return None
    return p


def in_base_game_library(content_root) -> bool:
    """
    Is this world filed under NO_TU - the game as it shipped, with no update?

    It matters because the base game does not use _MinecraftSaveInfo at all.
    A real NO_TU world is the world folder and its header, and nothing else;
    the index arrives with a later title update. Writing one there produces a
    file the base game never makes and cannot read.
    """
    for part in Path(content_root).parts:
        if part.upper() == BASE_GAME_DIR:
            return True
    return False


def register_world(content_root, folder_name: str, world_name: str,
                   thumbnail: bytes = None):
    """
    Add or update one world in the index, leaving the others alone.

    Matching is on the folder, so converting the same world twice updates its
    entry rather than listing it twice. The header is written alongside,
    because the index alone does not make the world appear in the game.

    Under NO_TU there is no index - see in_base_game_library - so only the
    header is written, and _MinecraftSaveInfo.header is not created either.
    """
    if in_base_game_library(content_root):
        write_save_headers(content_root, folder_name, world_name,
                           with_index=False)
        return []

    entries = read_saveinfo(content_root)
    for e in entries:
        if e['folder'].lower() == folder_name.lower():
            e['name'] = world_name
            if thumbnail:
                e['thumbnail'] = thumbnail
            break
    else:
        entries.append({'name': world_name, 'folder': folder_name,
                        'thumbnail': thumbnail})
    write_saveinfo(content_root, entries)
    write_save_headers(content_root, folder_name, world_name)
    return entries


def unique_world_folder(content_root, base: str = None) -> str:
    """
    A .bin folder name that is safe and unique in *content_root*.

    A folder name with spaces, parentheses or other punctuation crashes an
    older title update at world-select, so the name is stripped to letters and
    digits only. A console world with no usable name becomes Save<digits> - the
    convention the game itself uses; a named cross-platform world keeps its name
    with the spaces removed. A clash gets a numeric suffix, so a second
    "New World" lands next to the first as NewWorld2.bin, not on top of it.
    """
    import random
    stem = re.sub(r'[^A-Za-z0-9]', '', base or '')
    if not stem:
        stem = 'Save' + ''.join(random.choices('0123456789', k=12))
    existing = set()
    try:
        existing = {p.name.lower() for p in Path(content_root).glob('*.bin')}
    except OSError:
        pass
    cand, n = stem, 1
    while (cand + '.bin').lower() in existing:
        n += 1
        cand = f'{stem}{n}'
    return cand + '.bin'


def downgrade_thumbnail(png: bytes) -> bytes:
    """
    A Nexia __thumbnail.png cut back to what an old title update can read.

    The tEXt chunk gained keys over the years - 4J_HOSTOPTIONS, 4J_TEXTUREPACK,
    4J_EXTRADATA, 4J_#LOADS. An older build's world-select parses that chunk and
    crashes on a key it does not know; a genuine old thumbnail carries only
    4J_SEED. So keep the image and the seed, drop the rest. Anything that is not
    a valid PNG is returned untouched.
    """
    import zlib
    if not png or png[:8] != PNG_MAGIC:
        return png

    def chunk(typ, data):
        return (struct.pack('>I', len(data)) + typ + data
                + struct.pack('>I', zlib.crc32(typ + data) & 0xFFFFFFFF))
    try:
        seed, keep, i = None, [], 8
        while i < len(png):
            ln = struct.unpack_from('>I', png, i)[0]
            typ = png[i + 4: i + 8]
            data = png[i + 8: i + 8 + ln]
            i += 12 + ln
            if typ == b'tEXt':
                parts = data.split(b'\x00')
                if parts and parts[0] == b'4J_SEED' and len(parts) > 1:
                    seed = parts[1]
                continue
            keep.append((typ, data))
        out = bytearray(PNG_MAGIC)
        for typ, data in keep:
            if typ == b'IEND' and seed is not None:
                out += chunk(b'tEXt', b'4J_SEED\x00' + seed)
            out += chunk(typ, data)
        return bytes(out)
    except Exception:
        return png


def history_current_edition(folder):
    """
    What a world is NOW, from its conversion history, or None.

    The last line records the most recent conversion, and its right-hand side
    is what the world became. That is more reliable than the folder it sits
    in, which only names a title update by convention.
    """
    info, lines = read_history(folder)
    if info.get('Current edition'):
        return info['Current edition']
    if not lines:
        return None
    tail = lines[-1].split('->')[-1].strip()
    return tail or None


def history_title_update(folder):
    """The title update from a world's history, as an int, or None."""
    now = history_current_edition(folder)
    if not now:
        return None
    m = re.search(r'TU(\d+)', now)
    return int(m.group(1)) if m else None


# Account files sit at a known depth under an emulator install:
#   content/<xuid>/FFFE07D1/00010000/<xuid>/Account
# An unbounded rglob for them is a disaster on a USB drive root - it walks
# the entire volume, blocks the worker, and every later world silently fails
# to load because the thread never comes back. Bound it.
ACCOUNT_MAX_DEPTH = 6


def _find_accounts(root):
    """Account files under *root*, without walking the whole volume."""
    root = Path(root)
    for base in (root / 'content', root / 'Content', root):
        if not base.is_dir():
            continue
        stack = [(base, 0)]
        while stack:
            folder, depth = stack.pop()
            if depth > ACCOUNT_MAX_DEPTH:
                continue
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_file() and entry.name == 'Account':
                        yield entry
                    elif entry.is_dir():
                        stack.append((entry, depth + 1))
                except OSError:
                    continue
        if base != root:
            return


_PROFILE_CACHE = {}


def _profiles_cached(root) -> list:
    """list_profiles, remembered - scanning for Account files is not free."""
    key = str(root)
    if key not in _PROFILE_CACHE:
        try:
            _PROFILE_CACHE[key] = list_profiles(root)
        except Exception:
            _PROFILE_CACHE[key] = []
    return _PROFILE_CACHE[key]


def list_profiles(root, kind: str = 'nexia360') -> list:
    """
    Every profile under an emulator install, newest-looking first.

    [{'xuid', 'gamertag', 'picture', 'signed_in'}]. signed_in marks the one
    the emulator loads on boot, which is the sensible default to convert into.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    booted = (nexia_profile_xuid(root) or '').upper()
    seen, out = set(), []
    for account in _find_accounts(root):
        if not account.is_file():
            continue
        xuid = account.parent.name.upper()
        if len(xuid) != 16 or xuid in seen:
            continue
        try:
            int(xuid, 16)
        except ValueError:
            continue
        seen.add(xuid)
        prof = {'xuid': xuid, 'gamertag': None, 'picture': None,
                'signed_in': xuid == booted}
        try:
            prof['gamertag'] = account_gamertag(account.read_bytes())
        except Exception:
            pass
        for tile in ('tile_64.png', 'tile_32.png'):
            pic = account.parent / tile
            if pic.is_file():
                data = pic.read_bytes()
                if data[:8] == PNG_MAGIC:
                    prof['picture'] = data
                    break
        out.append(prof)
    out.sort(key=lambda p: (not p['signed_in'], p['gamertag'] or p['xuid']))
    return out


def profile_label(prof: dict) -> str:
    """
    How a profile should read in a dropdown - just the gamertag.

    Which profile is signed in decides which one starts selected, not how
    it reads; saying so again in the text is noise once the right one is
    already picked.
    """
    return prof.get('gamertag') or prof.get('xuid') or 'unknown'


def profile_for_world_path(path) -> dict:
    """
    The profile a world belongs to, from the Content/<xuid>/ in its path.

    Returns {} when the path has no profile in it, which is the case for a
    loose .bin or a Windows LCE folder.
    """
    p = Path(path)
    xuid = profile_id_from_path(p)
    if not xuid:
        return {}
    # Walk up to whatever contains the emulator install, then read it there.
    for parent in p.parents:
        if (parent / 'Account').is_file():
            try:
                return {'xuid': xuid,
                        'gamertag': account_gamertag(
                            (parent / 'Account').read_bytes())}
            except Exception:
                break
        for account in parent.glob(f'**/{xuid}/Account'):
            try:
                return {'xuid': xuid,
                        'gamertag': account_gamertag(account.read_bytes())}
            except Exception:
                break
        if parent.name.lower() in ('library', 'content'):
            break
    return {'xuid': xuid, 'gamertag': None}


def rename_world(world: dict, new_name: str) -> str:
    """
    Rename a world everywhere the name is kept. Returns the name that stuck.

    The FOLDER is deliberately left alone. Its name is arbitrary - Nexia uses
    a timestamp - and both the index and the header record it, so renaming the
    folder would mean rewriting both of those just to keep them agreeing about
    something nobody reads. The display name is what the game shows, and it
    lives in three places that all have to be updated together or the world
    reverts to whatever the one we missed says.

    A .bin package is refused: the name is inside a signed STFS header, and
    editing it breaks the signature that makes the save loadable.
    """
    new_name = (new_name or '').strip()
    if not new_name:
        raise ValueError('A world needs a name.')

    path = Path(world.get('path') or '')
    if path.suffix.lower() == '.bin' and path.is_file():
        raise ValueError(
            'This world is a signed .bin package. Its name is inside the '
            'signature, so renaming it here would stop the console loading '
            'it. Convert it to an emulator folder first.')

    folder = path.parent if path.is_file() else path
    if not folder.is_dir():
        raise ValueError('That world is no longer on disk.')

    old = world.get('name') or folder.name
    content_root = folder.parent

    # The name lives in three places, and a rename that misses one leaves the
    # world reverting to whatever that one still says: the index the game
    # reads, the header beside it, and the history, which is our own store and
    # the one the world list reads for a Windows LCE world with neither of the
    # first two. note_rename writes the history entry.
    register_world(content_root, folder.name, new_name,
                   find_thumbnail(folder, EMU_THUMB_NAME))
    hdr = headers_dir(content_root) / (folder.name + SAVE_HEADER_SUFFIX)
    if hdr.is_file():
        try:
            hdr.write_bytes(build_save_header(folder.name, new_name))
        except OSError:
            pass
    note_rename(folder, old, new_name)
    return new_name


def delete_world(world: dict) -> bool:
    """
    Remove a world and the records that point at it.

    Leaving the index entry or the header behind would list a world that is
    not there, which reads as a broken save rather than a deleted one.
    """
    path = Path(world.get('path') or '')
    if not path.exists():
        return False

    if path.suffix.lower() == '.bin' and path.is_file():
        path.unlink()
        return True

    folder = path.parent if path.is_file() else path
    content_root = folder.parent
    name = folder.name

    shutil.rmtree(folder)
    hdr = headers_dir(content_root) / (name + SAVE_HEADER_SUFFIX)
    try:
        if hdr.is_file():
            hdr.unlink()
    except OSError:
        pass
    entries = [e for e in read_saveinfo(content_root)
               if e['folder'].lower() != name.lower()]
    try:
        write_saveinfo(content_root, entries)
    except OSError:
        pass
    return True


def note_rename(folder, old_name: str, new_name: str):
    """
    Record a rename in the history, and set the world's name there.

    For an emulator world the index and header are the name store and this is
    a log entry beside them. For a Windows LCE world there is no index and no
    header, so the history IS the name store - which is why this now writes
    even for a world that has no prior history, rather than giving up on it.
    """
    if not new_name or old_name == new_name:
        return False
    info, lines = read_history(folder)
    info['World name'] = new_name
    if old_name:
        lines.append(f"{len(lines) + 1}. renamed from {old_name} to "
                     f"{new_name}")
    write_history(folder, info, lines)
    return True


def history_last_known_name(folder):
    """
    The name this world had when it was last converted, or None.

    Used only to notice a rename - never to label the world.
    """
    _info, lines = read_history(folder)
    for line in reversed(lines):
        if ' renamed from ' in line and ' to ' in line:
            return line.split(' to ', 1)[1].strip()
        if '"' in line:
            parts = line.split('"')
            if len(parts) >= 2:
                return parts[1]
    return None


# ---------------------------------------------------------------------------
# Narrowing the title update from level.dat
#
# A save never records its title update - but it does gain fields over time,
# and a field that only ever appears from TU N onward is a hard lower bound.
# Measured across all 75 sample worlds: each of these is present in EVERY
# world at or after the update named, and in none before it. That contiguity
# is what makes them usable; a field that came and went would prove nothing.
#
# Combined with the save version's upper bound this turns "TU36 - TU68" into
# "TU46 - TU68" for a world carrying DataVersion, which is a real improvement
# on a blank cell.
# ---------------------------------------------------------------------------

LEVEL_FIELD_MIN_TU = {
    'GameType': 5, 'MapFeatures': 5, 'generatorName': 5, 'generatorVersion': 5,
    'hasBeenInCreative': 5, 'newSeaLevel': 5, 'spawnBonusChest': 5,
    'StrongholdX': 7, 'StrongholdY': 7, 'StrongholdZ': 7, 'hardcore': 7,
    'hasStronghold': 7,
    'StrongholdEndPortalX': 9, 'StrongholdEndPortalZ': 9,
    'hasStrongholdEndPortal': 9,
    'allowCommands': 14, 'initialized': 14,
    'HellScale': 17, 'XZSize': 17,
    'DayTime': 19,
    'Difficulty': 25, 'clearWeatherTime': 25,
    'DifficultyLocked': 31,
    'BiomeCentreXChunk': 36, 'BiomeCentreZChunk': 36, 'BiomeScale': 36,
    'DataVersion': 46, 'DimensionData': 46, 'ModernEnd': 46,
}


def level_field_names(payload: bytes, endian: str = '>') -> set:
    """Every top-level field name in a world's level.dat."""
    from . import lce_java
    for e in parse_payload(payload, endian)[4]:
        if e['filename'] != 'level.dat':
            continue
        raw = payload[e['start_offset']: e['start_offset'] + e['length']]
        try:
            import gzip
            raw = gzip.decompress(raw)
        except Exception:
            pass
        try:
            out = set()
            i = 3 + struct.unpack_from('>H', raw, 1)[0]
            i += 1
            i += 2 + struct.unpack_from('>H', raw, i)[0]
            while i < len(raw):
                t = raw[i]
                i += 1
                if t == 0:
                    break
                nl = struct.unpack_from('>H', raw, i)[0]
                out.add(raw[i + 2: i + 2 + nl].decode('utf-8', 'replace'))
                i += 2 + nl
                i = lce_java._skip(raw, i, t)
            return out
        except Exception:
            return set()
    return set()


def narrow_title_update(payload: bytes, endian: str = '>'):
    """
    (low, high) title updates a world can be, or None.

    Three independent things bound it, and the answer is where they overlap:
    the save version, the chunk storage version, and the fields present in
    level.dat. Save version 10 alone means TU36 - TU68, but a world whose
    chunks are version 10 cannot be past TU59 - so together they say
    TU36 - TU59. Nothing here guesses; every bound comes from a measurement
    across the full run of sample worlds.
    """
    cv = parse_payload(payload, endian)[3]
    span = SAVE_VERSION_SHORT.get(cv, '')
    hits = [int(n) for n in re.findall(r'TU(\d+)', span)]
    if not hits:
        return None
    low, high = (hits[0], hits[-1]) if len(hits) > 1 else (hits[0], hits[0])

    chunk = chunk_storage_version(payload, endian)
    bounds = CHUNK_VERSION_TITLE_UPDATES.get(chunk)
    if bounds:
        low = max(low, bounds[0])
        high = min(high, bounds[1])

    for field in level_field_names(payload, endian):
        floor = LEVEL_FIELD_MIN_TU.get(field)
        if floor is not None and floor > low:
            low = floor
    return (low, min(high, 75)) if low <= high else (low, high)


def title_update_text(payload: bytes, endian: str = '>') -> str:
    """'TU46 - TU68' or 'TU19', for showing in a list or on a page."""
    got = narrow_title_update(payload, endian)
    if not got:
        return ''
    low, high = got
    return f"TU{low}" if low == high else f"TU{low} - TU{high}"


def list_usb_profiles(drive) -> list:
    """
    Profiles on a memory unit or hard drive.

    A console stores each profile as a single package file, not the folder
    tree an emulator uses, so there is no Account to decrypt here - the XUID
    is the folder name under Content and that is all a drive gives us. The
    gamertag is left None rather than invented.
    """
    root = Path(drive)
    content = root / 'Content'
    if not content.is_dir():
        return []
    out = []
    try:
        children = sorted(content.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or len(child.name) != 16:
            continue
        try:
            int(child.name, 16)
        except ValueError:
            continue
        if child.name == '0000000000000000':
            continue                       # the all-users bucket, not a person
        # The profile package is often alongside the worlds; when it is,
        # the gamertag and gamerpic come out of it.
        out.append(read_usb_profile(root, child.name))
    return out


def profiles_for_target(kind: str, root) -> list:
    """
    Every profile a conversion could be filed under, for a given target.

    Emulators keep decryptable accounts; a drive only gives XUIDs. Either way
    the point is the same - the world has to land under the right profile or
    the game will not list it.
    """
    if not root:
        return []
    if kind == 'usb':
        return list_usb_profiles(root)
    return list_profiles(root)


def stfs_extract(raw: bytes, filename: str):
    """
    One file out of an STFS package, by name, or None.

    A console keeps a profile as a single signed package rather than the
    folder tree an emulator uses, so reading a gamertag off a memory unit
    means going in through the container first.
    """
    import sys
    pass  # vendored: sys.path hack removed
    from .converter import STFSPackage, _stfs_read_file
    try:
        pkg = STFSPackage(raw)
    except Exception:
        return None
    for name, start_block, size in pkg.file_table:
        if name.lower() == filename.lower():
            try:
                return _stfs_read_file(raw, start_block, size,
                                       pkg.table_shift)
            except Exception:
                return None
    return None


# A console profile package lives under the dashboard's own title id.
DASHBOARD_TITLE_ID_HEX = 'FFFE07D1'


def usb_profile_package(drive, xuid: str):
    """Where a drive keeps the package for one profile, if it is there."""
    p = (Path(drive) / 'Content' / xuid / DASHBOARD_TITLE_ID_HEX
         / '00010000' / xuid)
    return p if p.is_file() else None


def read_usb_profile(drive, xuid: str) -> dict:
    """
    {'xuid', 'gamertag', 'picture'} for a profile on a drive.

    The package is not always there - a world can sit under a XUID whose
    profile was never copied across - so gamertag and picture stay None
    rather than being invented when it is missing.
    """
    out = {'xuid': xuid.upper(), 'gamertag': None, 'picture': None,
           'signed_in': False}
    pkg_path = usb_profile_package(drive, xuid)
    if not pkg_path:
        return out
    try:
        raw = pkg_path.read_bytes()
    except OSError:
        return out
    account = stfs_extract(raw, 'Account')
    if account:
        try:
            out['gamertag'] = account_gamertag(account)
        except Exception:
            pass
    for tile in ('tile_64.png', 'tile_32.png'):
        pic = stfs_extract(raw, tile)
        if pic and pic[:8] == PNG_MAGIC:
            out['picture'] = pic
            break
    return out


def world_container(src):
    """
    The thing that actually *is* the world on disk.

    An emulator keeps a world as a folder - savegame.dat plus a thumbnail and
    a name file - so exporting only the .dat would throw away the name and
    the picture. A memory unit keeps it as one signed .bin, which is whole on
    its own. Returns (path, is_folder).
    """
    p = Path(src)
    if p.is_dir():
        return p, True
    if p.name.lower() in (EMU_DAT_NAME, 'savegame.dat', 'savedata.ms'):
        return p.parent, True
    return p, False


def export_world(src, dest_dir, name=None, log=None):
    """
    Copy a world out to anywhere, byte for byte, without converting it.

    Nothing is re-encoded and nothing is renamed inside the save, so the copy
    is the same world - it just lives somewhere else. An existing name is not
    overwritten; a numbered suffix is used instead.
    """
    out = log or (lambda *_a: None)
    container, is_folder = world_container(src)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    label = sanitise(name or read_worldname_txt(container)
                     or container.stem or container.name)
    target = dest_dir / (label if is_folder else label + container.suffix)
    n = 2
    while target.exists():
        stem = label + f' ({n})'
        target = dest_dir / (stem if is_folder else stem + container.suffix)
        n += 1

    if is_folder:
        shutil.copytree(container, target)
        files = sum(1 for _ in target.rglob('*') if _.is_file())
        out(f'Exported {files} files to {target}')
    else:
        shutil.copy2(container, target)
        out(f'Exported {target.stat().st_size:,} bytes to {target}')
    # Only a folder has somewhere of its own to keep a history. A .bin is one
    # file, and dropping a history beside it would litter whichever folder the
    # user happened to export into.
    if is_folder:
        append_history(target, 'exported', f'to {target}')
    return target


def detect_title_update_for_import(path) -> int:
    """
    The best title update for a world being imported, as a number.

    An exported world sits in a plain folder, so its PATH says nothing - which
    is why importing one used to fall straight to 19 and file a TU68 world
    under TU19. The order here is most-precise first:

      1. world_history.txt - our own converter writes the exact target there.
      2. the folder path - a real "Title Update N" folder.
      3. the save version - narrows to a range; the TOP of that range is the
         safest single number, since every block the world has exists there.
      4. 19, only when nothing else can be read at all.
    """
    p = Path(path)
    folder = p.parent if p.is_file() else p
    from_history = history_title_update(folder)
    if from_history is not None:
        return from_history
    from_path = title_update_from_path(path)
    if from_path is not None:
        return from_path
    try:
        payload, _n, _t = read_console_input(str(path), 'xbox360')
        span = narrow_title_update(payload, '>')
        if span:
            return span[1]                 # top of the range
    except Exception:
        pass
    return 19


def confirm_title_update(path, tu):
    """
    (title_update, range_text) for a world, with the save allowed to overrule.

    A folder or file name is a label somebody typed; the save version and
    level.dat are evidence. Where the two disagree the save wins - a world
    called "TU19" that is really a TU14 world is precisely the case a
    converter must not take on trust. Where they agree the name is kept,
    because it is the more precise of the two.

    A world that cannot be read at all falls back to the name, since a label
    is still better than nothing.
    """
    try:
        payload, _n, _t = read_console_input(path, 'xbox360')
        span = narrow_title_update(payload, '>')
    except Exception:
        return tu, ''
    if not span:
        return tu, ''
    low, high = span
    text = f"TU{low}" if low == high else f"TU{low} - TU{high}"
    if tu is not None and low <= tu <= high:
        return tu, text
    if low == high:
        return low, text
    return None, text


# ---------------------------------------------------------------------------
# Importing a world into a location
#
# A world that is not already under a configured folder has to be COPIED into
# one before it can appear in a list - the list is a view of what is on disk,
# not a scratchpad. Where exactly it goes depends on the profile it is filed
# under, which is why importing has to ask.
# ---------------------------------------------------------------------------

# Where an imported world goes on a drive that has no console layout yet.
# A memory unit straight out of a package has no Content folder, and inventing
# a device id to fabricate one would produce a tree the console still would not
# read - the id has to match the actual hardware. So the world is parked
# somewhere obvious instead, and the user is told plainly.
IMPORT_HOLDING_FOLDER = 'Imported Minecraft Worlds'


def import_destination(kind: str, root, xuid: str = None,
                       title_update: int = 19, world_name: str = None):
    """
    (path, console_readable) for a world being imported into a location.

    *console_readable* is False when the world had to be parked in a holding
    folder rather than filed under a real profile, which is what happens on a
    drive with no Content folder. It still shows up in this tool's lists; it
    just will not show up on a console until it is moved under a profile.
    """
    root = Path(root)
    if kind in ('nexia360', 'xenia') or xuid:
        # Same refusal as converting, worded for the page that asked: being
        # told to "convert" on the import page is a small thing that makes the
        # message read as boilerplate rather than as an instruction.
        try:
            return emulator_destination(root, kind,
                                        title_update=title_update,
                                        world_name=world_name,
                                        xuid=xuid), True
        except TitleUpdateNotInstalled as exc:
            raise TitleUpdateNotInstalled(exc.title_update, exc.root,
                                          doing='import') from None
    holding = root / IMPORT_HOLDING_FOLDER
    base = sanitise(world_name or 'World')
    dest, n = holding / base, 2
    while dest.exists():
        dest = holding / f'{base} ({n})'
        n += 1
    return dest, False


def import_world(src, kind: str, root, xuid: str = None,
                 title_update: int = 19, world_name: str = None, log=None):
    """
    Copy a world into a location so it appears in that location's list.

    Nothing is converted - this is the same byte-for-byte copy export makes,
    pointed inwards. An emulator destination is registered in the save index
    as well, or the game would not list it however right the folder is.

    Returns (path, console_readable).
    """
    out = log or (lambda *_a: None)
    name = world_name or world_name_for(src)
    dest, readable = import_destination(kind, root, xuid=xuid,
                                        title_update=title_update,
                                        world_name=name)
    container, is_folder = world_container(src)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if is_folder:
        shutil.copytree(container, dest)
    else:
        # A single .bin is a world in its own right; it keeps its own name.
        dest = dest.with_suffix(container.suffix)
        shutil.copy2(container, dest)
    out(f'Imported to {dest}')

    if readable and is_folder:
        try:
            register_world(dest.parent, dest.name, name,
                           find_thumbnail(dest, EMU_THUMB_NAME))
            out(f'  {SAVEINFO_NAME}  [ok]  ({name})')
        except Exception as exc:
            out(f'  {SAVEINFO_NAME}  [skipped: {type(exc).__name__}]')

    append_history(dest if is_folder else dest.parent,
                   'imported', describe_edition('xbox360', title_update),
                   world_name=name)
    return dest, readable


# ---------------------------------------------------------------------------
# The Windows LCE player name
#
# Windows LCE has no account and no profile - it is a game running out of a
# directory. The only place a name exists at all is on the command line
# somebody launches it with:
#
#     D:\Games\LCEWindows64\Minecraft.Client.exe -ip 26.244.4.1 -name "Hijack Assassin"
#
# which in practice lives in a .lnk shortcut. A .lnk is a documented binary
# format (MS-SHLLINK), so the arguments can be read straight out of it - no
# COM, no shell automation, nothing that needs the file to be launched.
#
#     0x00  u32   header length, always 0x4C
#     0x04  16     the link CLSID
#     0x14  u32   LinkFlags - bit 0 HasLinkTargetIDList
#                             bit 1 HasLinkInfo
#                             bit 2 HasName
#                             bit 3 HasRelativePath
#                             bit 4 HasWorkingDir
#                             bit 5 HasArguments
#                             bit 7 IsUnicode
#
# After the 0x4C header come the optional structures in flag order, each a
# u16 character count followed by the string. Walking them in order is the
# only way to reach the arguments, because every one before it is variable
# length.
# ---------------------------------------------------------------------------

LNK_HEADER_SIZE = 0x4C
LNK_FLAG_TARGET_IDLIST = 1 << 0
LNK_FLAG_LINK_INFO = 1 << 1
LNK_FLAG_UNICODE = 1 << 7

# The string fields, in the order they are stored, with the flag that guards
# each. Arguments is the one worth having; the rest are walked past.
LNK_STRINGS = [('name', 1 << 2), ('relative_path', 1 << 3),
               ('working_dir', 1 << 4), ('arguments', 1 << 5),
               ('icon_location', 1 << 6)]

_NAME_ARG_RE = re.compile(r'-name\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)


def read_shortcut(path) -> dict:
    """
    {'target', 'arguments'} out of a .lnk, or {} if it is not one.

    Never raises: a shortcut that is malformed, truncated or simply not a
    shortcut is not an error, it is just not a source of a player name.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}
    if len(raw) < LNK_HEADER_SIZE:
        return {}
    if struct.unpack_from('<I', raw, 0)[0] != LNK_HEADER_SIZE:
        return {}

    flags = struct.unpack_from('<I', raw, 0x14)[0]
    at = LNK_HEADER_SIZE
    try:
        if flags & LNK_FLAG_TARGET_IDLIST:
            at += 2 + struct.unpack_from('<H', raw, at)[0]
        if flags & LNK_FLAG_LINK_INFO:
            at += struct.unpack_from('<I', raw, at)[0]

        out = {}
        unicode_strings = bool(flags & LNK_FLAG_UNICODE)
        for field, flag in LNK_STRINGS:
            if not flags & flag:
                continue
            count = struct.unpack_from('<H', raw, at)[0]
            at += 2
            width = 2 if unicode_strings else 1
            blob = raw[at: at + count * width]
            at += count * width
            out[field] = blob.decode('utf-16-le' if unicode_strings else 'mbcs',
                                     'replace')
    except (struct.error, IndexError):
        return {}
    return out


def player_name_from_shortcuts(root, max_depth: int = 3):
    """
    The -name a Windows LCE shortcut launches the game with, or None.

    Two places, because those are the two people actually use: the game's own
    folder - including a few levels above the saves, since the configured path
    points at GameHDD and the shortcut sits at the top of the install - and
    the desktop, where most shortcuts end up.

    Not the whole machine. A shortcut to this install names this install's
    player; one somewhere else names somebody else's.
    """
    import os
    root = Path(root)
    places = [root] + list(root.parents)[:3]
    for env in ('USERPROFILE', 'PUBLIC'):
        base = os.environ.get(env)
        if base:
            places.append(Path(base) / 'Desktop')
    seen = set()
    for base in places:
        if not base or base in seen or not base.is_dir():
            continue
        seen.add(base)
        stack = [(base, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for child in entries:
                if child.is_dir() and depth < max_depth:
                    stack.append((child, depth + 1))
                    continue
                if child.suffix.lower() != '.lnk':
                    continue
                link = read_shortcut(child)
                target = (link.get('relative_path') or '') + ' ' \
                    + (link.get('arguments') or '')
                if 'minecraft' not in target.lower():
                    continue
                hit = _NAME_ARG_RE.search(link.get('arguments') or '')
                if hit:
                    return (hit.group(1) or hit.group(2)).strip()
    return None
