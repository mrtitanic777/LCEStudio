"""Minimal STFS (Xbox 360 CON/LIVE/PIRS package) reader — enough to extract the
files (savegame.dat, thumbnail) out of a Minecraft: Xbox 360 save package.

Block/hash-tree math ported from Velocity (github.com/hetelek/Velocity,
XboxInternals/Stfs/StfsPackage.cpp). Handles the hash-table interspersing and
primary/backup table selection so fragmented files extract correctly."""
import hashlib
import os
import struct

BLOCK = 0x1000

# ---- metadata field offsets (XContent header) ----
OFF_HEADER_HASH = 0x32C          # SHA1(0x344 .. first_hash)
OFF_HEADER_SIZE = 0x340
OFF_CONTENT_TYPE = 0x344
OFF_TITLE_ID = 0x360
OFF_SAVEGAME_ID = 0x368
OFF_CONSOLE_ID = 0x36C           # 5 bytes
OFF_PROFILE_ID = 0x371           # 8 bytes (the account XUID)
OFF_DEVICE_ID = 0x3FD            # 0x14 bytes


def _first_hash(d):
    return (struct.unpack_from(">I", d, OFF_HEADER_SIZE)[0] + 0xFFF) & 0xFFFFF000


def _xuid_bytes(x):
    """Accept an XUID as int, hex string ('E0000...'), or bytes -> 8 bytes BE."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x[:8]).rjust(8, b"\x00")
    if isinstance(x, str):
        return bytes.fromhex(x.strip().lower().replace("0x", "").rjust(16, "0"))[:8]
    return struct.pack(">Q", int(x) & 0xFFFFFFFFFFFFFFFF)


def fix_header_hash(d):
    """Recompute the header/master hash @0x32C = SHA1(0x344 .. first-hash-table).
    Must be called after any header-metadata edit."""
    d[OFF_HEADER_HASH:OFF_HEADER_HASH + 0x14] = \
        hashlib.sha1(bytes(d[OFF_CONTENT_TYPE:_first_hash(d)])).digest()


def reassign_profile(con_bytes, new_profile_id, zero_device=True, kv=None):
    """Reassign a CON saved-game to a new account (XUID).

    The old account is gone but the user has a new one: set the Profile ID to the
    new account's XUID and (by default) zero the Console/Device IDs so the save
    isn't bound to the dead account's console. Only header metadata changes, so
    the whole data hash-tree stays valid -- we just recompute the header hash.

    The result loads on EMULATORS (Nexia360) and RGH/JTAG consoles, which don't
    verify the RSA signature. A stock RETAIL console additionally needs an RSA
    resign with that console's KV.bin -- pass `kv=` (path or bytes) to do it."""
    d = bytearray(con_bytes)
    if d[:4] not in (b"CON ", b"LIVE", b"PIRS"):
        raise ValueError("not an STFS package (magic %r)" % bytes(d[:4]))
    if d[:4] != b"CON ":
        raise ValueError("%s packages are strong-signed by Microsoft and cannot "
                         "be re-signed or reassigned" % d[:4].decode())
    d[OFF_PROFILE_ID:OFF_PROFILE_ID + 8] = _xuid_bytes(new_profile_id)
    if zero_device:
        d[OFF_CONSOLE_ID:OFF_CONSOLE_ID + 5] = b"\x00" * 5
        d[OFF_DEVICE_ID:OFF_DEVICE_ID + 0x14] = b"\x00" * 0x14
    fix_header_hash(d)
    if kv is not None:
        resign_con(d, kv)
    return bytes(d)


def resign_con(d, kv):
    """RSA-resign a CON header with a console KeyVault (KV.bin).

    NOT implemented on purpose: a valid CON signature needs the private key from a
    specific console's KV.bin, and the resulting package is valid ONLY on that
    console. The reassignment + rehash above already produces a package that loads
    on Nexia360 and RGH/JTAG consoles (which don't verify the RSA signature). If
    you must target a stock RETAIL console, resign the reassigned file in
    Velocity/Horizon with that console's KV -- a one-click step there."""
    raise NotImplementedError(resign_con.__doc__.strip())


def unpack_con(con, out_dir):
    """Extract an STFS CON/LIVE/PIRS saved-game package into the emulator's UNPACKED
    folder form: the inner file(s) (savegame.dat) + a `__thumbnail.png` written from
    the package's embedded thumbnail. This is the shape Nexia360 enumerates (it scans
    the profile's 00000001\\ dir for save FOLDERS, not raw STFS package files). Returns
    {name, profile, files}. `con` may be a path or raw bytes."""
    raw = con if isinstance(con, (bytes, bytearray)) else open(con, "rb").read()
    s = STFS(raw)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for nm in s.files:
        base = nm.replace("\\", "/").split("/")[-1]
        with open(os.path.join(out_dir, base), "wb") as f:
            f.write(s.read_file(nm))
        written.append(base)
    if s.thumbnail:
        with open(os.path.join(out_dir, "__thumbnail.png"), "wb") as f:
            f.write(s.thumbnail)
        written.append("__thumbnail.png")
    return {"name": s.display_name, "profile": s.profile_id, "files": written}


def is_con(path):
    """True if `path` is a file whose first 4 bytes are an STFS magic."""
    try:
        with open(path, "rb") as f:
            return f.read(4) in (b"CON ", b"LIVE", b"PIRS")
    except (OSError, IsADirectoryError, PermissionError):
        return False


class _Geom:
    """STFS block/hash geometry for a MALE (read-write, sex=1) package with PRIMARY
    hash tables active -- the exact addressing the STFS reader uses, so anything laid
    out through it reads back correctly. Handles top_level 0 (<=170 blocks) and 1
    (<=28900 blocks); saved games never exceed that."""
    def __init__(self, alloc_blocks, first_hash):
        self.sex = 1
        self.step = [0xAC, 0x723A]
        self.alloc_blocks = alloc_blocks
        self.first_hash = first_hash
        self.top_level = 0 if alloc_blocks <= 0xAA else 1

    def cbdbn(self, b):                                       # data block -> backing block
        r = (((b + 0xAA) // 0xAA) << 1) + b
        if b < 0xAA:
            return r
        r += ((b + 0x70E4) // 0x70E4) << 1
        return r if b < 0x70E4 else r + 2

    def block_addr(self, b):
        return (self.cbdbn(b) << 0xC) + self.first_hash

    def l0_backing(self, b):                                  # backing block of b's L0 table
        if b < 0xAA:
            return 0
        return (b // 0xAA) * self.step[0] + 2

    def l1_backing(self):                                     # backing block of the L1 table
        return self.step[0]

    def hash_addr(self, b):                                   # byte addr of b's L0 hash entry
        return (self.l0_backing(b) << 0xC) + self.first_hash + (b % 0xAA) * 0x18


def build_con(folder, out_path, template, display_name=None, profile_id=None):
    """Pack an unpacked save FOLDER (savegame.dat [+ other inner files]) back into a
    loadable STFS **CON** package. `template` is any existing CON (path or bytes)
    whose header skeleton -- console certificate, title name, metadata layout -- is
    cloned; only the payload, hash tree, file table and per-save metadata are rebuilt.

    The result loads on emulators (Nexia360) and RGH/JTAG consoles, which don't verify
    the RSA console signature. A stock RETAIL console additionally needs the package
    re-signed with that console's KV.bin (not available here) -- same limit as
    `resign_con`. Returns out_path.

    display_name/profile_id override the cloned values (else the template's are kept)."""
    tpl = template if isinstance(template, (bytes, bytearray)) else open(template, "rb").read()
    if tpl[:4] not in (b"CON ", b"LIVE", b"PIRS"):
        raise ValueError("template is not an STFS package")
    first_hash = (struct.unpack_from(">I", tpl, 0x340)[0] + 0xFFF) & 0xFFFFF000
    header = bytearray(tpl[:first_hash])                      # clone cert + metadata skeleton
    header[:4] = b"CON "

    # -- gather inner files (savegame.dat etc.); __thumbnail.png goes in the header --
    thumb = None
    inner = []
    if os.path.isdir(folder):
        names = sorted(os.listdir(folder))
        for nm in names:
            p = os.path.join(folder, nm)
            if not os.path.isfile(p):
                continue
            data = open(p, "rb").read()
            if nm == "__thumbnail.png":
                thumb = data
            else:
                inner.append((nm, data))
    else:                                                    # a lone savegame.dat file
        inner.append((os.path.basename(folder), open(folder, "rb").read()))
    if not inner:
        raise ValueError("no inner files to pack in %r" % folder)

    # -- assign data blocks: block 0 = file table, then each file's content --
    entries = []                                             # (name, start, nblocks, size)
    contents = []                                            # data-block-index -> 0x1000 chunk
    ft_nblocks = max(1, (len(inner) + 0x3F) // 0x40)
    cur = ft_nblocks                                         # file data starts after the FT
    for nm, data in inner:
        nb = max(1, (len(data) + 0xFFF) // 0x1000)
        entries.append((nm, cur, nb, len(data)))
        for i in range(nb):
            contents.append(data[i * 0x1000:(i + 1) * 0x1000].ljust(0x1000, b"\x00"))
        cur += nb
    alloc_blocks = cur

    # -- file-table block(s) at block 0.. --
    ftbuf = bytearray(ft_nblocks * 0x1000)
    for i, (nm, start, nb, size) in enumerate(entries):
        e = ftbuf[i * 0x40:(i + 1) * 0x40]
        nb_name = nm.encode("latin1", "ignore")[:0x28]
        ftbuf[i * 0x40:i * 0x40 + len(nb_name)] = nb_name
        ftbuf[i * 0x40 + 0x28] = len(nb_name)                # name len (no dir bit)
        struct.pack_into("<I", ftbuf, i * 0x40 + 0x29, nb)   # blocks (u24 in low 3 bytes)
        struct.pack_into("<I", ftbuf, i * 0x40 + 0x2C, nb)   # allocated blocks
        struct.pack_into("<I", ftbuf, i * 0x40 + 0x2F, start)  # start block (u24)
        struct.pack_into(">H", ftbuf, i * 0x40 + 0x32, 0xFFFF)  # path indicator (root)
        struct.pack_into(">I", ftbuf, i * 0x40 + 0x34, size)   # size (BE)
        ftbuf[i * 0x40 + 0x2B] = 0                            # keep u24 clean
        ftbuf[i * 0x40 + 0x2E] = 0
        ftbuf[i * 0x40 + 0x31] = 0
    # data-block list: FT blocks first (0..ft_nblocks-1), then file contents
    blocks = [ftbuf[i * 0x1000:(i + 1) * 0x1000] for i in range(ft_nblocks)] + contents

    # -- next-block chains --
    nextb = {}
    for j in range(ft_nblocks):
        nextb[j] = j + 1 if j < ft_nblocks - 1 else 0xFFFFFF
    for nm, start, nb, size in entries:
        for i in range(nb):
            nextb[start + i] = start + i + 1 if i < nb - 1 else 0xFFFFFF

    g = _Geom(alloc_blocks, first_hash)
    # -- size the buffer: max backing block over data + hash tables (+backup) --
    max_back = 0
    for b in range(alloc_blocks):
        max_back = max(max_back, g.cbdbn(b), g.l0_backing(b) + 1)
    if g.top_level == 1:
        max_back = max(max_back, g.l1_backing() + 1)
    buf = bytearray(first_hash + (max_back + 1) * 0x1000)
    buf[:len(header)] = header

    # -- write data blocks --
    for b in range(alloc_blocks):
        off = g.block_addr(b)
        buf[off:off + 0x1000] = blocks[b]

    # -- L0 hash tables (primary + male backup copy) --
    ngroups = (alloc_blocks + 0xA9) // 0xAA
    l0_hashes = []                                           # SHA1 of each L0 table block
    for grp in range(ngroups):
        base = (g.l0_backing(grp * 0xAA) << 0xC) + first_hash
        for k in range(0xAA):
            b = grp * 0xAA + k
            if b >= alloc_blocks:
                break
            ea = base + k * 0x18
            buf[ea:ea + 0x14] = hashlib.sha1(blocks[b]).digest()
            buf[ea + 0x14] = 0xC0                            # allocated data block
            nb = nextb.get(b, 0xFFFFFF)
            buf[ea + 0x15] = (nb >> 16) & 0xFF
            buf[ea + 0x16] = (nb >> 8) & 0xFF
            buf[ea + 0x17] = nb & 0xFF
        table = bytes(buf[base:base + 0x1000])
        buf[base + 0x1000:base + 0x2000] = table            # backup copy
        l0_hashes.append(hashlib.sha1(table).digest())

    # -- top hash: L1 table (top_level 1) or the single L0 table (top_level 0) --
    if g.top_level == 1:
        l1 = (g.l1_backing() << 0xC) + first_hash
        for i, h in enumerate(l0_hashes):
            ea = l1 + i * 0x18
            buf[ea:ea + 0x14] = h
            buf[ea + 0x14] = 0x80                           # hash-table entry
        table = bytes(buf[l1:l1 + 0x1000])
        buf[l1 + 0x1000:l1 + 0x2000] = table                # backup copy
        top_hash = hashlib.sha1(table).digest()
    else:
        top_hash = l0_hashes[0]

    # -- volume descriptor + metadata --
    buf[0x379] = 0x24                                        # descriptor length
    buf[0x37B] = 0x00                                        # block separation (male, primary)
    struct.pack_into("<H", buf, 0x37C, ft_nblocks)          # file-table block count
    buf[0x37E] = 0; buf[0x37F] = 0; buf[0x380] = 0          # file-table block number = 0
    buf[0x381:0x381 + 0x14] = top_hash
    struct.pack_into(">I", buf, 0x395, alloc_blocks)        # total allocated blocks
    struct.pack_into(">I", buf, 0x399, 0)                   # total unallocated blocks
    struct.pack_into(">I", buf, 0x344, 1)                   # content type = Saved Game
    struct.pack_into(">Q", buf, 0x34C, alloc_blocks * 0x1000)  # content size
    if profile_id is not None:
        buf[OFF_PROFILE_ID:OFF_PROFILE_ID + 8] = _xuid_bytes(profile_id)
    if display_name is not None:
        nm = display_name.encode("utf-16-be")[:0x7E]
        buf[0x411:0x411 + 0x80] = nm.ljust(0x80, b"\x00")
    if thumb is not None:
        struct.pack_into(">I", buf, 0x1712, len(thumb))
        buf[0x171A:0x171A + len(thumb)] = thumb

    fix_header_hash(buf)                                     # metadata self-hash @0x32C
    with open(out_path, "wb") as f:
        f.write(bytes(buf))
    return out_path


class STFS:
    def __init__(self, data):
        d = self.d = data
        if d[:4] not in (b"CON ", b"LIVE", b"PIRS"):
            raise ValueError("not an STFS package (magic %r)" % d[:4])
        self.header_size = struct.unpack_from(">I", d, 0x340)[0]
        self.first_hash = (self.header_size + 0xFFF) & 0xFFFFF000
        self.block_sep = d[0x37B]
        self.ft_block_count = struct.unpack_from("<H", d, 0x37C)[0]
        self.ft_block_number = d[0x37E] | (d[0x37F] << 8) | (d[0x380] << 16)
        self.alloc_blocks = struct.unpack_from(">I", d, 0x395)[0]
        self.sex = (~self.block_sep) & 1                      # 0=Female, 1=Male
        self.step = [0xAB, 0x718F] if self.sex == 0 else [0xAC, 0x723A]
        self.top_level = 0 if self.alloc_blocks <= 0xAA else (1 if self.alloc_blocks <= 0x70E4 else 2)
        self._load_top_table()
        self.files = self._read_file_listing()
        # metadata — the WORLD name is displayName @0x411; titleName @0x1691 = "Minecraft"
        self.display_name = self._wstr(0x411)            # the world's name
        self.title_name = self._wstr(0x1691)             # the game title
        self.profile_id = d[OFF_PROFILE_ID:OFF_PROFILE_ID + 8].hex()  # XUID the save is bound to
        self.thumbnail = self._thumbnail()

    def _wstr(self, off, n=0x80):
        b = self.d[off:off + n]
        z = b.find(b"\x00\x00")
        return b[:z if z >= 0 else n].decode("utf-16-be", "ignore").rstrip("\x00")

    # --- block / hash math (Velocity) ---
    def _cbdbn(self, b):                                      # ComputeBackingDataBlockNumber
        r = (((b + 0xAA) // 0xAA) << self.sex) + b
        if b < 0xAA:
            return r
        r += ((b + 0x70E4) // 0x70E4) << self.sex
        return r if b < 0x70E4 else r + (1 << self.sex)

    def _block_addr(self, b):
        return (self._cbdbn(b) << 0xC) + self.first_hash

    def _cl0(self, b):                                       # ComputeLevel0BackingHashBlockNumber
        if b < 0xAA:
            return 0
        num = (b // 0xAA) * self.step[0]
        num += ((b // 0x70E4) + 1) << self.sex
        return num if b // 0x70E4 == 0 else num + (1 << self.sex)

    def _cl1(self, b):
        if b < 0x70E4:
            return self.step[0]
        return (1 << self.sex) + (b // 0x70E4) * self.step[1]

    def _load_top_table(self):
        d = self.d
        true_block = (self._cl0(0) if self.top_level == 0 else
                      self._cl1(0) if self.top_level == 1 else self.step[1])
        base = (true_block << 0xC) + self.first_hash
        addr = base + ((self.block_sep & 2) << 0xB)
        per = [1, 0xAA, 0x70E4][self.top_level]
        cnt = self.alloc_blocks // per
        if self.alloc_blocks > 0x70E4 and self.alloc_blocks % 0x70E4:
            cnt += 1
        elif self.alloc_blocks > 0xAA and self.alloc_blocks % 0xAA:
            cnt += 1
        self.top_entries = []
        for i in range(cnt + 1):
            e = d[addr + i * 0x18: addr + i * 0x18 + 0x18]
            if len(e) < 0x18:
                break
            self.top_entries.append(e[0x14])                 # status byte

    def _hash_addr(self, b):
        a = (self._cl0(b) << 0xC) + self.first_hash + (b % 0xAA) * 0x18
        if self.top_level == 0:
            a += (self.block_sep & 2) << 0xB
        elif self.top_level == 1:
            a += (self.top_entries[b // 0xAA] & 0x40) << 6
        else:
            l1 = (self.top_entries[b // 0x70E4] & 0x40) << 6
            pos = (self._cl1(b) << 0xC) + self.first_hash + l1 + (b % 0xAA) * 0x18
            a += (self.d[pos + 0x14] & 0x40) << 6
        return a

    def _next_block(self, b):
        a = self._hash_addr(b)
        return (self.d[a + 0x15] << 16) | (self.d[a + 0x16] << 8) | self.d[a + 0x17]

    def _read_chain(self, start, nblocks, size):
        out = bytearray()
        cur = start
        for _ in range(nblocks):
            o = self._block_addr(cur)
            out += self.d[o:o + BLOCK]
            cur = self._next_block(cur)
            if cur == 0xFFFFFF or cur == 0:
                break
        return bytes(out[:size])

    def _read_file_listing(self):
        files = {}
        cur = self.ft_block_number
        for _ in range(self.ft_block_count):
            o = self._block_addr(cur)
            block = self.d[o:o + BLOCK]
            for i in range(BLOCK // 0x40):
                e = block[i * 0x40:(i + 1) * 0x40]
                nlen = e[0x28] & 0x3F
                if nlen == 0:
                    continue
                name = e[:nlen].decode("latin1", "ignore")
                blocks = e[0x29] | (e[0x2A] << 8) | (e[0x2B] << 16)
                start = e[0x2F] | (e[0x30] << 8) | (e[0x31] << 16)
                size = struct.unpack_from(">I", e, 0x34)[0]
                is_dir = bool(e[0x28] & 0x80)
                if not is_dir:
                    files[name] = (start, blocks, size)
            cur = self._next_block(cur)
            if cur == 0xFFFFFF or cur == 0:
                break
        return files

    def read_file(self, name):
        start, blocks, size = self.files[name]
        return self._read_chain(start, blocks, size)

    def _thumbnail(self):
        size = struct.unpack_from(">I", self.d, 0x1712)[0]
        img = self.d[0x171A:0x171A + max(0, size)]
        return img if img[:4] == b"\x89PNG" else None


if __name__ == "__main__":
    import sys
    s = STFS(open(sys.argv[1], "rb").read())
    print("display_name=%r sex=%d top_level=%d alloc_blocks=%d first_hash=0x%X"
          % (s.display_name, s.sex, s.top_level, s.alloc_blocks, s.first_hash))
    for nm, (st, bl, sz) in s.files.items():
        print("  file %-24s start=%d blocks=%d size=%d" % (nm, st, bl, sz))
