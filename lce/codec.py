#!/usr/bin/env python3
"""
Xbox 360 Minecraft: Legacy Console Edition savegame unpacker.

Container:  [u32 version=0][u32 BE decompressed_size] then XMemCompress LZX chunks.
Chunk framing (per chunk):
    if first byte == 0xFF:  0xFF, u16 BE raw_size, u16 BE comp_size
    else:                   u16 BE comp_size,  raw_size defaults to 0x8000
Each chunk is an INDEPENDENT LZX stream that inflates to raw_size bytes.

LZX core reimplemented from the libmspack lzxd algorithm (MS-LZX / WIM flavour).
"""
import sys, struct

# ---- LZX constants -----------------------------------------------------------
MIN_MATCH = 2
NUM_CHARS = 256
BLOCKTYPE_VERBATIM   = 1
BLOCKTYPE_ALIGNED    = 2
BLOCKTYPE_UNCOMPRESSED = 3
PRETREE_NUM_ELEMENTS = 20
ALIGNED_NUM_ELEMENTS = 8
NUM_PRIMARY_LENGTHS  = 7
NUM_SECONDARY_LENGTHS = 249

# position-slot extra-bits / base tables ---------------------------------------
def _build_pos_tables(num_slots):
    extra = []
    for i in range(num_slots + 1):
        if i < 4:
            extra.append(0)
        else:
            extra.append(min((i >> 1) - 1, 17))
    base = [0] * (num_slots + 1)
    for i in range(1, num_slots + 1):
        base[i] = base[i - 1] + (1 << extra[i - 1])
    return extra, base

def _num_position_slots(window):
    extra = [0, 0, 0, 0]
    base = [0, 1, 2, 3]
    i = 4
    while base[-1] < window:
        e = min((i >> 1) - 1, 17)
        extra.append(e)
        base.append(base[-1] + (1 << extra[i - 1]))
        i += 1
    return len(base) - 1   # slots 0..(len-2)

# ---- LZX bit reader: 16-bit little-endian words, consumed MSB-first ----------
class BitReader:
    __slots__ = ("d", "p", "n", "acc", "nbits")
    def __init__(self, data):
        self.d = data
        self.n = len(data)
        self.p = 0
        self.acc = 0
        self.nbits = 0

    def _ensure(self, k):
        while self.nbits < k:
            p = self.p
            if p + 1 < self.n:
                w = self.d[p] | (self.d[p + 1] << 8)
            elif p < self.n:
                w = self.d[p]
            else:
                w = 0
            self.p = p + 2
            self.acc = (self.acc << 16) | w
            self.nbits += 16

    def bits(self, k):
        if k == 0:
            return 0
        if self.nbits < k:
            self._ensure(k)
        self.nbits -= k
        v = (self.acc >> self.nbits) & ((1 << k) - 1)
        self.acc &= (1 << self.nbits) - 1
        return v

    def peek(self, k):
        if self.nbits < k:
            self._ensure(k)
        return (self.acc >> (self.nbits - k)) & ((1 << k) - 1)

    def remove(self, k):
        self.nbits -= k
        self.acc &= (1 << self.nbits) - 1

    def align_to_16(self):
        # drop remaining bits in the current 16-bit word
        r = self.nbits & 15
        if r:
            self.remove(r)

    def read_raw_byte(self):
        # after 16-bit alignment, pull bytes straight from the stream buffer
        if self.nbits >= 8:
            self.nbits -= 8
            v = (self.acc >> self.nbits) & 0xFF
            self.acc &= (1 << self.nbits) - 1
            return v
        p = self.p
        b = self.d[p] if p < self.n else 0
        self.p = p + 1
        return b

# ---- canonical Huffman table (MSB-first) ------------------------------------
class Huffman:
    __slots__ = ("maxlen", "table", "lens")
    def __init__(self, lens):
        self.lens = lens
        maxlen = 0
        for L in lens:
            if L > maxlen:
                maxlen = L
        self.maxlen = maxlen
        if maxlen == 0:
            self.table = None
            return
        size = 1 << maxlen
        table = [0] * size            # packs (sym<<5)|len ; len<=16 fits in 5 bits
        code = 0
        # assign canonical codes ordered by (length, symbol)
        by_len = [[] for _ in range(maxlen + 1)]
        for sym, L in enumerate(lens):
            if L:
                by_len[L].append(sym)
        for L in range(1, maxlen + 1):
            for sym in by_len[L]:
                start = code << (maxlen - L)
                end = start + (1 << (maxlen - L))
                entry = (sym << 5) | L
                for j in range(start, end):
                    table[j] = entry
                code += 1
            code <<= 1
        self.table = table

    def decode(self, br):
        if br.nbits < self.maxlen:
            br._ensure(self.maxlen)
        peek = (br.acc >> (br.nbits - self.maxlen)) & ((1 << self.maxlen) - 1)
        entry = self.table[peek]
        L = entry & 31
        br.nbits -= L
        br.acc &= (1 << br.nbits) - 1
        return entry >> 5

# ---- length-table reader (pretree RLE, delta mod 17) ------------------------
def read_lengths(lens, first, last, br):
    pre_lens = [br.bits(4) for _ in range(PRETREE_NUM_ELEMENTS)]
    pre = Huffman(pre_lens)
    i = first
    while i < last:
        sym = pre.decode(br)
        if sym == 17:
            run = br.bits(4) + 4
            while run and i < last:
                lens[i] = 0; i += 1; run -= 1
        elif sym == 18:
            run = br.bits(5) + 20
            while run and i < last:
                lens[i] = 0; i += 1; run -= 1
        elif sym == 19:
            run = br.bits(1) + 4
            sym2 = pre.decode(br)
            val = (lens[i] - sym2) % 17
            while run and i < last:
                lens[i] = val; i += 1; run -= 1
        else:
            lens[i] = (lens[i] - sym) % 17
            i += 1

# ---- LZX decode: frame-based (32 KB output frames, 16-bit realign per frame) -
FRAME = 0x8000

def lzx_decompress_chunk(data, out_len, num_slots, extra, base, tolerant=False):
    """Decode an XMemCompress LZX stream to out_len bytes. With tolerant=True,
    a decode error (corrupt/truncated stream) drops the partially-decoded frame
    and returns whatever decoded cleanly so far, instead of raising — used by the
    recovery tool to salvage a damaged save."""
    br = BitReader(data)
    out = bytearray()
    R0 = R1 = R2 = 1
    main_elems = NUM_CHARS + (num_slots << 3)
    main_lens = [0] * main_elems
    length_lens = [0] * (NUM_SECONDARY_LENGTHS + 1)
    main = length = aligned = None
    block_type = 0
    block_length = 0
    block_remaining = 0
    header_read = False
    e8 = 0
    e8_size = 0

    while len(out) < out_len:
        frame_start = len(out)
        try:
            # ---- frame start ----
            if not header_read:
                e8 = br.bits(1)
                if e8:
                    hi = br.bits(16); lo = br.bits(16)
                    e8_size = (hi << 16) | lo
                header_read = True

            frame_size = min(FRAME, out_len - len(out))
            target = len(out) + frame_size

            while len(out) < target:
                if block_remaining == 0:
                    # realign after an odd-length uncompressed block
                    if block_type == BLOCKTYPE_UNCOMPRESSED and (block_length & 1):
                        br.read_raw_byte()
                    block_type = br.bits(3)
                    block_length = (br.bits(8) << 16) | (br.bits(8) << 8) | br.bits(8)
                    block_remaining = block_length

                    if block_type == BLOCKTYPE_ALIGNED:
                        aln_lens = [br.bits(3) for _ in range(ALIGNED_NUM_ELEMENTS)]
                        aligned = Huffman(aln_lens)
                        read_lengths(main_lens, 0, NUM_CHARS, br)
                        read_lengths(main_lens, NUM_CHARS, main_elems, br)
                        main = Huffman(main_lens)
                        read_lengths(length_lens, 0, NUM_SECONDARY_LENGTHS, br)
                        length = Huffman(length_lens)
                    elif block_type == BLOCKTYPE_VERBATIM:
                        aligned = None
                        read_lengths(main_lens, 0, NUM_CHARS, br)
                        read_lengths(main_lens, NUM_CHARS, main_elems, br)
                        main = Huffman(main_lens)
                        read_lengths(length_lens, 0, NUM_SECONDARY_LENGTHS, br)
                        length = Huffman(length_lens)
                    elif block_type == BLOCKTYPE_UNCOMPRESSED:
                        br.align_to_16()
                        R0 = (br.read_raw_byte() | (br.read_raw_byte() << 8)
                              | (br.read_raw_byte() << 16) | (br.read_raw_byte() << 24))
                        R1 = (br.read_raw_byte() | (br.read_raw_byte() << 8)
                              | (br.read_raw_byte() << 16) | (br.read_raw_byte() << 24))
                        R2 = (br.read_raw_byte() | (br.read_raw_byte() << 8)
                              | (br.read_raw_byte() << 16) | (br.read_raw_byte() << 24))
                    else:
                        raise ValueError("bad block type %d at out=%d" % (block_type, len(out)))

                this_run = block_remaining
                if this_run > target - len(out):
                    this_run = target - len(out)
                block_remaining -= this_run

                if block_type == BLOCKTYPE_UNCOMPRESSED:
                    for _ in range(this_run):
                        out.append(br.read_raw_byte())
                    continue

                produced = 0
                while produced < this_run:
                    sym = main.decode(br)
                    if sym < NUM_CHARS:
                        out.append(sym)
                        produced += 1
                        continue
                    sym -= NUM_CHARS
                    len_head = sym & 7
                    pos_slot = sym >> 3
                    if len_head == NUM_PRIMARY_LENGTHS:
                        match_len = length.decode(br) + NUM_PRIMARY_LENGTHS + MIN_MATCH
                    else:
                        match_len = len_head + MIN_MATCH

                    if pos_slot == 0:
                        offset = R0
                    elif pos_slot == 1:
                        offset = R1; R1 = R0; R0 = offset
                    elif pos_slot == 2:
                        offset = R2; R2 = R0; R0 = offset
                    else:
                        eb = extra[pos_slot]
                        if aligned is not None and eb >= 3:
                            verb = br.bits(eb - 3) << 3
                            aln = aligned.decode(br)
                            offset = base[pos_slot] + verb + aln - 2
                        else:
                            verb = br.bits(eb)
                            offset = base[pos_slot] + verb - 2
                        R2 = R1; R1 = R0; R0 = offset

                    src = len(out) - offset
                    if src < 0:                       # corrupt stream: match before start
                        raise IndexError("match offset %d before output start" % offset)
                    for _ in range(match_len):
                        out.append(out[src])
                        src += 1
                    produced += match_len
                # a match may cross the frame target; account for the overshoot
                if produced > this_run:
                    block_remaining -= (produced - this_run)

            # ---- end of frame: realign input bitstream to a 16-bit boundary ----
            if br.nbits > 0:
                br._ensure(16)
            rem = br.nbits & 15
            if rem:
                br.remove(rem)
        except (IndexError, ValueError):
            if not tolerant:
                raise
            del out[frame_start:]         # discard the partial corrupt frame, stop
            break

    del out[out_len:]
    size = len(out)
    if e8 and size > 10:
        _undo_e8(out, size, e8_size)
    return bytes(out)

def _undo_e8(out, size, e8_size):
    # LZX intel-call translation, 32KB frames, first e8_size bytes only
    i = 0
    limit = min(size - 10, 0x8000 - 10) if size >= 10 else 0
    while i < limit:
        if out[i] == 0xE8:
            rel = struct.unpack_from("<i", out, i + 1)[0]
            if -i <= rel < e8_size:
                if rel >= 0:
                    abs_ = rel - i
                else:
                    abs_ = rel + e8_size
                struct.pack_into("<i", out, i + 1, abs_ & 0xFFFFFFFF)
            i += 5
        else:
            i += 1

# ---- container --------------------------------------------------------------
def unpack(path):
    d = open(path, "rb").read()
    ver = struct.unpack_from(">I", d, 0)[0]
    dec_size = struct.unpack_from(">I", d, 4)[0]
    window = 0x20000                      # XMemCompress LZX window = 128 KB
    num_slots = _num_position_slots(window)
    extra, base = _build_pos_tables(num_slots)

    # Concatenate every chunk's compressed payload into one continuous LZX
    # bitstream (framing is 32 KB output frames; LZX blocks span frames).
    payload = bytearray()
    off = 8
    n = len(d)
    ci = 0
    while off < n:
        b = d[off]
        if b == 0xFF:
            comp = struct.unpack_from(">H", d, off + 3)[0]
            off += 5
        else:
            comp = struct.unpack_from(">H", d, off)[0]
            off += 2
        if comp == 0:
            break
        payload += d[off:off + comp]
        off += comp
        ci += 1

    sys.stderr.write("version=%d  decompressed_size=%d  window=%#x  slots=%d  chunks=%d  comp_payload=%d\n"
                     % (ver, dec_size, window, num_slots, ci, len(payload)))

    out = lzx_decompress_chunk(bytes(payload), dec_size, num_slots, extra, base)
    sys.stderr.write("produced=%d / %d\n" % (len(out), dec_size))
    return out

# ---- savegame VFS (the decompressed container) ------------------------------
_VFS_ENTRY = 136          # 128-byte UTF-16BE name + u32 BE length + u32 BE offset

def list_savegame(blob):
    """Parse the mini-VFS index -> list of (name, offset, length)."""
    index_off = struct.unpack_from(">I", blob, 0)[0]
    files = []
    p = index_off
    while p + _VFS_ENTRY <= len(blob):
        raw = blob[p:p + 128]
        z = raw.find(b"\x00\x00")
        if z < 0:
            z = len(raw)
        if z & 1:
            z += 1
        name = raw[:z].decode("utf-16-be", "replace").rstrip("\x00")
        length = struct.unpack_from(">I", blob, p + 128)[0]
        offset = struct.unpack_from(">I", blob, p + 132)[0]
        if not name:
            break
        files.append((name, offset, length))
        p += _VFS_ENTRY
    return files

def extract_savegame(path, outdir):
    """Decompress an LCE savegame and write its VFS files into outdir.
    Returns the list of (name, offset, length) entries."""
    import os
    blob = unpack(path)
    os.makedirs(outdir, exist_ok=True)
    files = list_savegame(blob)
    for name, off, length in files:
        if off + length <= len(blob):
            with open(os.path.join(outdir, name.replace("/", "_")), "wb") as fh:
                fh.write(blob[off:off + length])
    return files

# ---- region files (.mcr) ----------------------------------------------------
# Layout:  [4096 B location table][4096 B timestamps][chunk sectors]
#   location entry = u32 BE (sector << 8) | sector_count
#   chunk sector   = [u32 BE 0x80000000|comp_len][u32 BE decompressed_size]
#                    then XMemCompress-LZX -> RLE-compressed legacy chunk NBT.
_REGION_WINDOW = 0x20000

def rle_decode(data):
    """LCE region-chunk RLE (mirror of Minecraft.World DecompressRLE):
       0..254     -> literal byte
       255,k(<=2) -> (k+1) copies of 0xFF
       255,n(>=3),b -> (n+1) copies of b."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0xFF:
            k = data[i + 1]
            if k <= 2:
                out += b"\xff" * (k + 1); i += 2
            else:
                out += bytes([data[i + 2]]) * (k + 1); i += 3
        else:
            out.append(b); i += 1
    return bytes(out)

def _inflate_chunk(sector_bytes):
    """One chunk's sector bytes -> its decompressed payload (legacy NBT starting with
    0x0A, OR a v8/v9/v10 compressed-storage blob starting with its version short) and
    the declared decompressed size. No format expansion -- just LZX+RLE inflate."""
    comp_len = struct.unpack_from(">I", sector_bytes, 0)[0] & 0x7FFFFFFF
    dec_size = struct.unpack_from(">I", sector_bytes, 4)[0]
    body = sector_bytes[8:8 + comp_len]
    # XMemCompress LZX frame stream: full 32 KB frames use a short [u16 comp]
    # header, the final partial frame a long [0xFF][u16 raw][u16 comp] header.
    # Concatenate every frame's payload, tracking total RLE output length.
    num_slots = _num_position_slots(_REGION_WINDOW)
    extra, base = _build_pos_tables(num_slots)
    payload = bytearray()
    raw_total = 0
    off, n = 0, len(body)
    while off < n:
        if body[off] == 0xFF:
            raw = struct.unpack_from(">H", body, off + 1)[0]
            clen = struct.unpack_from(">H", body, off + 3)[0]; off += 5
        else:
            raw = 0x8000
            clen = struct.unpack_from(">H", body, off)[0]; off += 2
        if clen == 0:                       # hit the 5-byte trailer
            break
        payload += body[off:off + clen]; off += clen
        raw_total += raw
    rle = lzx_decompress_chunk(bytes(payload), raw_total, num_slots, extra, base)
    return rle_decode(rle), dec_size


def _storage_kind(nbt):
    """Classify an inflated chunk payload: 'legacy' (0x0A NBT), 'compressed' (v8/9/10
    tile storage, plus the v11/0x0B "Elytra" format which shares that layout), or
    'other' (e.g. an Aquatic format-12 0x0C blob handled elsewhere)."""
    if nbt[:1] == b"\x0a":
        return "legacy"
    if len(nbt) >= 2 and struct.unpack_from(">h", nbt, 0)[0] in (
            COMPRESSED_STORAGE_V8, COMPRESSED_STORAGE_V9, COMPRESSED_STORAGE_V10,
            COMPRESSED_STORAGE_V11):
        return "compressed"
    return "other"


def decode_region_chunk(sector_bytes, full_height=False):
    """One chunk's sector bytes -> decompressed legacy chunk NBT.

    Handles both LCE chunk encodings: plain legacy NBT (payload starts with
    a Compound tag 0x0A) and the version 8/9/10/11 "compressed chunk storage"
    (palettized tile storage + sparse nibble storage) which is expanded here
    into the same legacy chunk NBT.

    full_height keeps a compressed chunk's upper 128 (y128..255) -- pass True for
    the 256-tall 3D viewer, leave False for the 128-tall TU0 path."""
    nbt, dec_size = _inflate_chunk(sector_bytes)
    if _storage_kind(nbt) == "compressed":
        nbt = build_legacy_chunk_nbt(decode_compressed_chunk(nbt), full_height=full_height)
    return nbt, dec_size


def region_has_compressed(region_raw, sample=8):
    """True if any of the first `sample` present chunks in a raw .mcr region use the
    v8/v9/v10 compressed-storage format (i.e. this came from a TU that TU0 can't read
    without a full re-serialize to legacy). Cheap: inflates at most `sample` chunks."""
    seen = 0
    for i in range(1024):
        loc = struct.unpack_from(">I", region_raw, i * 4)[0]
        sector = loc >> 8
        if sector == 0:
            continue
        p = sector * 4096
        try:
            nbt, _ = _inflate_chunk(region_raw[p:])
        except Exception:
            continue
        if _storage_kind(nbt) == "compressed":
            return True
        seen += 1
        if seen >= sample:
            break
    return False

# ---- version 8/9 "compressed chunk storage" ---------------------------------
COMPRESSED_STORAGE_V8 = 8      # blocks/data/light, no InhabitedTime
COMPRESSED_STORAGE_V9 = 9      # adds InhabitedTime
COMPRESSED_STORAGE_V10 = 10    # same layout as v9 (verified: y0 all-bedrock, offsets clean)
COMPRESSED_STORAGE_V11 = 11    # "Elytra" (0x0B): same tile-storage layout as v10 (verified on
                               # real TU54-era chunks -- y0 all-bedrock, superflat dirt column)
BLOCKS_PER_SECTION = 128 * 16 * 16          # 32768
NIBBLES_PER_SECTION = BLOCKS_PER_SECTION // 2  # 16384
_IDX_TYPE_MASK = 0x0003
_IDX_0OR8 = 0x0003
_IDX_0BIT_FLAG = 0x0004
_SPARSE_ALL_ZERO = 128
_SPARSE_ALL_FIFTEEN = 129

def _compressed_tile_index(block, tile):
    idx = ((block & 0x180) << 6) | ((block & 0x060) << 4) | ((block & 0x01F) << 2)
    idx |= ((tile & 0x30) << 7) | ((tile & 0x0C) << 5) | (tile & 0x03)
    return idx

def _read_compressed_tile_storage(payload, off):
    allocated = struct.unpack_from(">i", payload, off)[0]; off += 4
    blob = payload[off:off + allocated]; off += allocated
    data_region = blob[1024:]
    blocks = bytearray(BLOCKS_PER_SECTION)
    for block in range(512):
        block_index = struct.unpack_from("<H", blob, block * 2)[0]
        itype = block_index & _IDX_TYPE_MASK
        if itype == _IDX_0OR8:
            if block_index & _IDX_0BIT_FLAG:                 # whole 4x4x4 tile one value
                value = (block_index >> 8) & 0xFF
                for tile in range(64):
                    blocks[_compressed_tile_index(block, tile)] = value
            else:                                            # 8-bit: 64 literal bytes
                doff = (block_index >> 1) & 0x7FFE
                for tile in range(64):
                    blocks[_compressed_tile_index(block, tile)] = data_region[doff + tile]
            continue
        # 1/2/4-bit palette
        bits = 1 << itype
        type_count = 1 << bits
        type_mask = type_count - 1
        index_shift = 3 - itype
        index_mask_bits = 7 >> itype
        index_mask_bytes = 62 >> index_shift
        packed_size = 8 << itype
        doff = (block_index >> 1) & 0x7FFE
        palette = data_region[doff:doff + type_count]
        packed = data_region[doff + type_count:doff + type_count + packed_size]
        for tile in range(64):
            bidx = (tile >> index_shift) & index_mask_bytes
            bit = (tile & index_mask_bits) * bits
            pi = (packed[bidx] >> bit) & type_mask
            blocks[_compressed_tile_index(block, tile)] = palette[pi]
    return bytes(blocks), off

def _set_nibble(nib, xz, y, value):
    pos = (xz << 7) | y
    slot = pos >> 1
    if pos & 1:
        nib[slot] = (nib[slot] & 0x0F) | ((value & 0x0F) << 4)
    else:
        nib[slot] = (nib[slot] & 0xF0) | (value & 0x0F)

def _read_sparse_nibble(payload, off, supports_fifteen):
    count = struct.unpack_from(">i", payload, off)[0]; off += 4
    storage = 128 + count * 128
    blob = payload[off:off + storage]; off += storage
    plane_indices = blob[:128]
    plane_data = blob[128:]
    nib = bytearray(NIBBLES_PER_SECTION)
    for y in range(128):
        pidx = plane_indices[y]
        if pidx == _SPARSE_ALL_ZERO:
            continue
        if supports_fifteen and pidx == _SPARSE_ALL_FIFTEEN:
            for xz in range(256):
                _set_nibble(nib, xz, y, 15)
            continue
        po = pidx * 128
        plane = plane_data[po:po + 128]
        for xz in range(128):
            packed = plane[xz]
            _set_nibble(nib, xz << 1, y, packed & 0x0F)
            _set_nibble(nib, (xz << 1) + 1, y, (packed >> 4) & 0x0F)
    return bytes(nib), off

def decode_compressed_chunk(payload):
    """Version 8/9 compressed chunk storage -> dict of legacy chunk arrays."""
    off = 0
    version = struct.unpack_from(">h", payload, off)[0]; off += 2
    cx = struct.unpack_from(">i", payload, off)[0]; off += 4
    cz = struct.unpack_from(">i", payload, off)[0]; off += 4
    last_update = struct.unpack_from(">q", payload, off)[0]; off += 8
    inhabited = 0
    if version >= COMPRESSED_STORAGE_V9:
        inhabited = struct.unpack_from(">q", payload, off)[0]; off += 8
    blocks, off = _read_compressed_tile_storage(payload, off)
    blocks_hi, off = _read_compressed_tile_storage(payload, off)          # upper 128 (y128..255)
    data, off = _read_sparse_nibble(payload, off, False)
    data_hi, off = _read_sparse_nibble(payload, off, False)               # upper
    sky, off = _read_sparse_nibble(payload, off, True)
    sky_hi, off = _read_sparse_nibble(payload, off, True)                 # upper
    blocklight, off = _read_sparse_nibble(payload, off, True)
    blocklight_hi, off = _read_sparse_nibble(payload, off, True)          # upper
    heightmap = payload[off:off + 256]; off += 256
    terrain_flags = struct.unpack_from(">h", payload, off)[0]; off += 2
    biomes = payload[off:off + 256]; off += 256
    dynamic = payload[off:]      # trailing dynamic NBT root, or empty
    return dict(version=version, xPos=cx, zPos=cz, LastUpdate=last_update,
                InhabitedTime=inhabited, Blocks=blocks, Data=data, SkyLight=sky,
                BlockLight=blocklight, BlocksHi=blocks_hi, DataHi=data_hi,
                SkyLightHi=sky_hi, BlockLightHi=blocklight_hi, HeightMap=heightmap,
                TerrainPopulatedFlags=terrain_flags, Biomes=biomes, dynamic=dynamic)

def _nbt_name(name):
    nb = name.encode("utf-8")
    return struct.pack(">H", len(nb)) + nb

def build_legacy_chunk_nbt(c, full_height=False):
    """Build a big-endian legacy chunk NBT (root -> Level) from decoded arrays.
    Any entities/tile-entities in the trailing dynamic NBT are spliced in.

    full_height: emit the full 256-tall chunk as [lower-128 | upper-128] (the layout
    the 3D viewer's _store_old_nbt expects), so builds above y127 survive. Default
    False keeps the 128-tall lower section only -- correct for the TU0 (128-tall)
    downgrade path and any consumer that only wants the classic height."""
    def _col(k, khi):
        lo = c[k]
        hi = c.get(khi)
        return lo + hi if (full_height and hi is not None) else lo

    level = bytearray()
    level += b"\x03" + _nbt_name("xPos") + struct.pack(">i", c["xPos"])
    level += b"\x03" + _nbt_name("zPos") + struct.pack(">i", c["zPos"])
    level += b"\x04" + _nbt_name("LastUpdate") + struct.pack(">q", c["LastUpdate"])
    level += b"\x04" + _nbt_name("InhabitedTime") + struct.pack(">q", c["InhabitedTime"])
    cols = {"Blocks": _col("Blocks", "BlocksHi"), "Data": _col("Data", "DataHi"),
            "SkyLight": _col("SkyLight", "SkyLightHi"),
            "BlockLight": _col("BlockLight", "BlockLightHi"), "HeightMap": c["HeightMap"]}
    for k in ("Blocks", "Data", "SkyLight", "BlockLight", "HeightMap"):
        level += b"\x07" + _nbt_name(k) + struct.pack(">i", len(cols[k])) + cols[k]
    level += b"\x02" + _nbt_name("TerrainPopulatedFlags") + struct.pack(">h", c["TerrainPopulatedFlags"])
    level += b"\x07" + _nbt_name("Biomes") + struct.pack(">i", len(c["Biomes"])) + c["Biomes"]
    # splice inner tags of the trailing dynamic root (Entities/TileEntities/...)
    dyn = c.get("dynamic") or b""
    if len(dyn) >= 3 and dyn[0] == 0x0A:
        nl = struct.unpack_from(">H", dyn, 1)[0]
        inner = dyn[3 + nl:]
        if inner.endswith(b"\x00"):
            inner = inner[:-1]
        level += inner
    else:
        level += b"\x09" + _nbt_name("Entities") + b"\x0a" + struct.pack(">i", 0)
        level += b"\x09" + _nbt_name("TileEntities") + b"\x0a" + struct.pack(">i", 0)
    out = bytearray()
    out += b"\x0a" + _nbt_name("")          # root compound (empty name)
    out += b"\x0a" + _nbt_name("Level")     # Level compound
    out += level
    out += b"\x00"                          # end Level
    out += b"\x00"                          # end root
    return bytes(out)

def decode_region(path, full_height=False):
    """Yield (chunk_x, chunk_z, nbt_bytes) for every present chunk in a .mcr.
    full_height=True keeps compressed chunks' upper 128 (for the 256-tall viewer)."""
    d = open(path, "rb").read()
    for i in range(1024):
        loc = struct.unpack_from(">I", d, i * 4)[0]
        sector = loc >> 8
        if sector == 0:
            continue
        cx, cz = i % 32, i // 32
        p = sector * 4096
        nbt, _ = decode_region_chunk(d[p:], full_height=full_height)
        yield cx, cz, nbt

# ---- ENCODE (write LCE saves) ----------------------------------------------
# LZX is emitted using uncompressed blocks only: a fully valid, decoder-
# accepted LZX stream that needs no match-finder/Huffman encoder. Output is
# therefore larger than the console's (which uses real LZX compression); the
# format and framing are correct and round-trip exactly. A real LZX compressor
# can later replace lzx_compress without changing the surrounding structure.

class _BitWriter:
    """Inverse of BitReader: bits accumulate MSB-first, flushed as 16-bit LE words."""
    __slots__ = ("out", "acc", "nbits")
    def __init__(self):
        self.out = bytearray(); self.acc = 0; self.nbits = 0
    def put(self, value, n):
        if n == 0:
            return
        self.acc = (self.acc << n) | (value & ((1 << n) - 1))
        self.nbits += n
        while self.nbits >= 16:
            self.nbits -= 16
            w = (self.acc >> self.nbits) & 0xFFFF
            self.out.append(w & 0xFF); self.out.append((w >> 8) & 0xFF)
            self.acc &= (1 << self.nbits) - 1
    def align16(self):
        if self.nbits & 15:
            self.put(0, 16 - (self.nbits & 15))
    def put_raw(self, data):        # only valid once bit buffer is word-flushed
        self.out += data

def _lzx_uncompressed_frames(data):
    """Fallback: (raw_size, frame_bytes) per 32 KB frame using LZX uncompressed
    blocks — always valid, no compression."""
    frames = []
    n = len(data)
    pos = 0
    first = True
    while pos < n or first:
        fsize = min(FRAME, n - pos)
        if fsize == 0 and not first:
            break
        bw = _BitWriter()
        if first:
            bw.put(0, 1)                 # E8 header bit = 0 (once)
            first = False
        bw.put(3, 3)                     # BLOCKTYPE_UNCOMPRESSED
        bw.put((fsize >> 16) & 0xFF, 8); bw.put((fsize >> 8) & 0xFF, 8); bw.put(fsize & 0xFF, 8)
        bw.align16()
        bw.put_raw(struct.pack("<III", 1, 1, 1))     # R0,R1,R2
        bw.put_raw(data[pos:pos + fsize])
        if fsize & 1:
            bw.put_raw(b"\x00")
        frames.append((fsize, bytes(bw.out)))
        pos += fsize
        if fsize < FRAME:
            break
    return frames

# ---- real LZX compressor (verbatim blocks: hash-chain matches + Huffman) ----
_ENC_SLOTS = _num_position_slots(_REGION_WINDOW)          # 34 for the 128 KB window
_ENC_EXTRA, _ENC_BASE = _build_pos_tables(_ENC_SLOTS)
_MAIN_ELEMS = NUM_CHARS + (_ENC_SLOTS << 3)
_MAX_OFFSET = _ENC_BASE[_ENC_SLOTS] - 3
_ENC_MIN_MATCH = 3
_ENC_MAX_MATCH = 257
_ENC_MAX_CHAIN = 64

def _package_merge(weights, maxbits):
    """Length-limited Huffman code lengths (<= maxbits), complete code."""
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [1]
    base = sorted(((weights[i], (i,)) for i in range(n)), key=lambda c: c[0])
    prev = []
    for _ in range(maxbits):
        packaged = [(prev[k][0] + prev[k + 1][0], prev[k][1] + prev[k + 1][1])
                    for k in range(0, len(prev) - 1, 2)]
        prev = sorted(base + packaged, key=lambda c: c[0])
    length = [0] * n
    for _w, syms in prev[:2 * n - 2]:
        for s in syms:
            length[s] += 1
    for i in range(n):
        if length[i] == 0:
            length[i] = 1
    return length

def _huff_lengths(freq, count, maxbits):
    lens = [0] * count
    used = [(s, f) for s, f in freq.items() if f > 0]
    if not used:
        return lens
    if len(used) == 1:
        lens[used[0][0]] = 1
        return lens
    ll = _package_merge([f for _s, f in used], maxbits)
    for (s, _f), L in zip(used, ll):
        lens[s] = L
    return lens

def _huff_codes(lengths):
    """Canonical codes matching the decoder's Huffman() assignment."""
    maxlen = max(lengths) if lengths else 0
    by_len = [[] for _ in range(maxlen + 1)]
    for sym, L in enumerate(lengths):
        if L:
            by_len[L].append(sym)
    codes = [0] * len(lengths)
    code = 0
    for L in range(1, maxlen + 1):
        for sym in by_len[L]:
            codes[sym] = code
            code += 1
        code <<= 1
    return codes

def _emit_lengths(bw, lens, first, last):
    """Serialize code lengths lens[first:last] via the pretree (prev == 0)."""
    tokens = []
    i = first
    while i < last:
        v = lens[i]
        if v == 0:
            j = i
            while j < last and lens[j] == 0:
                j += 1
            run = j - i
            while run >= 20:
                take = min(run, 51); tokens.append((18, 5, take - 20)); i += take; run -= take
            while run >= 4:
                take = min(run, 19); tokens.append((17, 4, take - 4)); i += take; run -= take
            while run > 0:
                tokens.append((0, 0, 0)); i += 1; run -= 1
        else:
            tokens.append(((-v) % 17, 0, 0)); i += 1
    freq = {}
    for sym, _n, _val in tokens:
        freq[sym] = freq.get(sym, 0) + 1
    pre_lens = _huff_lengths(freq, 20, 15)
    pre_codes = _huff_codes(pre_lens)
    for s in range(20):
        bw.put(pre_lens[s], 4)
    for sym, nbits, val in tokens:
        bw.put(pre_codes[sym], pre_lens[sym])
        if nbits:
            bw.put(val, nbits)

def _find_slot(formatted):
    lo, hi = 0, _ENC_SLOTS
    while lo + 1 < hi:
        mid = (lo + hi) >> 1
        if _ENC_BASE[mid] <= formatted:
            lo = mid
        else:
            hi = mid
    return lo

def _tokenize(data):
    n = len(data)
    head = {}
    prev = [0] * n
    tokens = []
    i = 0
    while i < n:
        best_len, best_off = 0, 0
        if i + _ENC_MIN_MATCH <= n:
            key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
            j = head.get(key, -1)
            chain = 0
            # never let a match cross a 32 KB output-frame boundary: LZX realigns
            # the bitstream per frame, so a boundary-crossing match desyncs.
            frame_edge = ((i >> 15) + 1) << 15
            limit = min(_ENC_MAX_MATCH, n - i, frame_edge - i)
            while j >= 0 and chain < _ENC_MAX_CHAIN:
                if i - j > _MAX_OFFSET:
                    break
                l = 0
                while l < limit and data[j + l] == data[i + l]:
                    l += 1
                if l > best_len:
                    best_len, best_off = l, i - j
                    if l >= limit:
                        break
                j = prev[j]
                chain += 1
        if best_len >= _ENC_MIN_MATCH:
            tokens.append((best_off, best_len))
            end = i + best_len
            while i < end:
                if i + _ENC_MIN_MATCH <= n:
                    key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                    prev[i] = head.get(key, -1); head[key] = i
                i += 1
        else:
            tokens.append((-1, data[i]))     # literal: (-1, byte)
            if i + _ENC_MIN_MATCH <= n:
                key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                prev[i] = head.get(key, -1); head[key] = i
            i += 1
    return tokens

def _lzx_real_frames(data):
    n = len(data)
    tokens = _tokenize(data)
    R0 = R1 = R2 = 1
    records = []
    main_freq = {}
    length_freq = {}
    for a, b in tokens:
        if a == -1:                          # literal
            main_freq[b] = main_freq.get(b, 0) + 1
            records.append((b, -1, 0, 0, 1))
            continue
        off, ml = a, b
        len_foot = ml - 2
        if len_foot >= 7:
            len_head = 7
            length_sym = len_foot - 7
            length_freq[length_sym] = length_freq.get(length_sym, 0) + 1
        else:
            len_head = len_foot
            length_sym = -1
        if off == R0:
            slot, nbits, val = 0, 0, 0
        elif off == R1:
            slot, nbits, val = 1, 0, 0; R1 = R0; R0 = off
        elif off == R2:
            slot, nbits, val = 2, 0, 0; R2 = R0; R0 = off
        else:
            formatted = off + 2
            slot = _find_slot(formatted)
            nbits = _ENC_EXTRA[slot]; val = formatted - _ENC_BASE[slot]
            R2 = R1; R1 = R0; R0 = off
        main_sym = 256 + (slot << 3) + len_head
        main_freq[main_sym] = main_freq.get(main_sym, 0) + 1
        records.append((main_sym, length_sym, nbits, val, ml))

    main_lens = _huff_lengths(main_freq, _MAIN_ELEMS, 16)
    length_lens = _huff_lengths(length_freq, NUM_SECONDARY_LENGTHS, 16)
    main_codes = _huff_codes(main_lens)
    length_codes = _huff_codes(length_lens)

    bw = _BitWriter()
    bw.put(0, 1)                              # E8 header bit = 0
    bw.put(BLOCKTYPE_VERBATIM, 3)
    bw.put((n >> 16) & 0xFF, 8); bw.put((n >> 8) & 0xFF, 8); bw.put(n & 0xFF, 8)
    _emit_lengths(bw, main_lens, 0, NUM_CHARS)
    _emit_lengths(bw, main_lens, NUM_CHARS, _MAIN_ELEMS)
    _emit_lengths(bw, length_lens, 0, NUM_SECONDARY_LENGTHS)

    boundaries = []
    produced = 0
    next_edge = FRAME
    for main_sym, length_sym, nbits, val, out_len in records:
        bw.put(main_codes[main_sym], main_lens[main_sym])
        if length_sym != -1:
            bw.put(length_codes[length_sym], length_lens[length_sym])
        if nbits:
            bw.put(val, nbits)
        produced += out_len
        if produced >= next_edge:
            bw.align16()
            boundaries.append((next_edge, len(bw.out)))
            next_edge += FRAME
    bw.align16()
    if not boundaries or boundaries[-1][0] < n:
        boundaries.append((n, len(bw.out)))

    stream = bytes(bw.out)
    frames = []
    prev_byte = prev_out = 0
    for edge_out, edge_byte in boundaries:
        frames.append((edge_out - prev_out, stream[prev_byte:edge_byte]))
        prev_byte, prev_out = edge_byte, edge_out
    return frames

def lzx_compress_frames(data):
    """(raw_size, frame_bytes) per 32 KB frame. Uses the real verbatim-block
    compressor, self-verifies by decoding, and falls back to uncompressed
    blocks if anything is off — so the output is always valid."""
    try:
        frames = _lzx_real_frames(data)
        stream = b"".join(fb for _r, fb in frames)
        if lzx_decompress_chunk(stream, len(data), _ENC_SLOTS, _ENC_EXTRA, _ENC_BASE) == data:
            return frames
    except Exception:
        pass
    return _lzx_uncompressed_frames(data)

def lzx_compress(data):
    """`data` -> one LZX stream (verbatim-block compressed, uncompressed fallback)."""
    return b"".join(fb for _raw, fb in lzx_compress_frames(data))

def rle_encode(data):
    """Inverse of rle_decode (mirror of Minecraft.World CompressRLE)."""
    out = bytearray()
    n = len(data)
    i = 0
    while i < n:
        b = data[i]
        run = 1
        while i + run < n and data[i + run] == b and run < 256:
            run += 1
        if b == 0xFF:
            out.append(0xFF); out.append(run - 1)
            if run > 3:
                out.append(0xFF)
        elif run < 4:
            out += bytes([b]) * run
        else:
            out.append(0xFF); out.append(run - 1); out.append(b)
        i += run
    return bytes(out)

def xmem_compress(blob):
    """Bytes following the 8-byte container header: per-frame chunk framing
    (short u16 header for full frames, 0xFF long header for the final one)."""
    out = bytearray()
    for raw, fb in lzx_compress_frames(blob):
        if raw == FRAME:
            out += struct.pack(">H", len(fb)) + fb
        else:
            out += b"\xff" + struct.pack(">H", raw) + struct.pack(">H", len(fb)) + fb
    return bytes(out)

def pack_savegame_blob(blob):
    """A raw decompressed container blob -> the compressed savegame file bytes."""
    return struct.pack(">II", 0, len(blob)) + xmem_compress(blob)

def encode_region_chunk(nbt):
    """Legacy chunk NBT -> (sector_bytes, sector_count).  RLE + a single 0xFF
    LZX segment, header 0x80000000|comp_len, padded to a 4096-byte sector."""
    rle = rle_encode(nbt)
    stream = lzx_compress(rle)
    if len(rle) > 0xFFFF or len(stream) > 0xFFFF:
        raise ValueError("chunk RLE/LZX segment exceeds 16-bit size")
    seg = b"\xff" + struct.pack(">H", len(rle)) + struct.pack(">H", len(stream)) + stream
    header = struct.pack(">I", 0x80000000 | len(seg)) + struct.pack(">I", len(nbt))
    body = header + seg
    body += b"\x00" * ((-len(body)) % 4096)
    return body, len(body) // 4096

def build_region(chunks):
    """chunks: dict[(cx,cz)] -> legacy chunk NBT  ->  .mcr file bytes."""
    loc = bytearray(4096)
    timestamps = bytearray(4096)
    sectors = bytearray()
    sector_no = 2
    for (cx, cz), nbt in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        data, cnt = encode_region_chunk(nbt)
        idx = cx + cz * 32
        struct.pack_into(">I", loc, idx * 4, (sector_no << 8) | cnt)
        sectors += data
        sector_no += cnt
    return bytes(loc) + bytes(timestamps) + bytes(sectors)

def build_savegame(files):
    """files: list of (name, bytes) -> compressed savegame file bytes.
    Container layout: [u32 index_off][u32 count][u32 1][files...][index]."""
    body = bytearray(12)
    entries = []
    for name, data in files:
        off = len(body)
        body += data
        body += b"\x00" * ((-len(body)) % 4)     # 4-byte align each file
        entries.append((name, off, len(data)))
    index_off = len(body)
    for name, off, length in entries:
        nb = name.encode("utf-16-be")[:128]
        body += nb + b"\x00" * (128 - len(nb))
        body += struct.pack(">I", length) + struct.pack(">I", off)
    struct.pack_into(">I", body, 0, index_off)
    struct.pack_into(">I", body, 4, len(entries))
    struct.pack_into(">I", body, 8, 1)
    return pack_savegame_blob(bytes(body))

def pack_savegame_dir(indir, outfile):
    """Pack every file in a directory into an LCE savegame container."""
    import os
    names = sorted(os.listdir(indir))
    files = []
    for name in names:
        p = os.path.join(indir, name)
        if os.path.isfile(p):
            files.append((name, open(p, "rb").read()))
    data = build_savegame(files)
    open(outfile, "wb").write(data)
    return files

# ---- CLI --------------------------------------------------------------------
_USAGE = """lce_savegame.py -- Xbox 360 / PS3 Minecraft (Legacy Console Edition) save codec

  decode:
    lce_savegame.py unpack   <savefile> [out.bin]      container -> raw decompressed blob
    lce_savegame.py extract  <savefile> [out_dir]      container -> VFS files (level.dat, .mcr, ...)
    lce_savegame.py region   <file.mcr> [out_dir]      region -> per-chunk NBT (chunk.X.Z.nbt)
  encode:
    lce_savegame.py pack     <in_dir>   <out_savefile> VFS files dir -> savegame container

  With no subcommand, the first argument is treated as `unpack`.
"""

def _main(argv):
    import os
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stderr.write(_USAGE); return 0
    cmd = argv[0]
    if cmd not in ("unpack", "extract", "region", "pack"):
        cmd, argv = "unpack", ["unpack"] + argv     # bare-path shorthand

    if cmd == "unpack":
        src = argv[1]
        dst = argv[2] if len(argv) > 2 else src + ".decompressed"
        data = unpack(src)
        open(dst, "wb").write(data)
        sys.stderr.write("wrote %s (%d bytes)\n" % (dst, len(data)))
        return 0

    if cmd == "extract":
        src = argv[1]
        outdir = argv[2] if len(argv) > 2 else src + "_extracted"
        files = extract_savegame(src, outdir)
        sys.stderr.write("extracted %d file(s) -> %s\n" % (len(files), outdir))
        for name, _o, length in files:
            sys.stderr.write("  %-34s %8d bytes\n" % (name, length))
        return 0

    if cmd == "region":
        path = argv[1]
        outdir = argv[2] if len(argv) > 2 else path + "_chunks"
        os.makedirs(outdir, exist_ok=True)
        n = 0
        for cx, cz, nbt in decode_region(path):
            open(os.path.join(outdir, "chunk.%d.%d.nbt" % (cx, cz)), "wb").write(nbt)
            n += 1
        sys.stderr.write("decoded %d chunks -> %s\n" % (n, outdir))
        return 0

    if cmd == "pack":
        indir, outfile = argv[1], argv[2]
        files = pack_savegame_dir(indir, outfile)
        sys.stderr.write("packed %d file(s) -> %s (%d bytes)\n"
                         % (len(files), outfile, os.path.getsize(outfile)))
        return 0

if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]) or 0)
