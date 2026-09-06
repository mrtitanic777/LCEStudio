"""Container / region write layer for LCE retail saves.

Consolidates the proven, hard-won I/O:
  * retail container read/build (VFS)      -- lossless
  * console-exact chunk encode             -- lzxc.exe + the MANDATORY 5-byte
                                              segment trailer (seglen = comp + 10);
                                              without it the console freezes at
                                              "Loading spawn area"
  * single-chunk region surgery
NBT compounds (mobs, spawners, items) are built with the real nbt engine.
"""
import os
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from . import codec, recover
from . import nbt as N

_HERE = os.path.dirname(os.path.abspath(__file__))
LZXC = os.path.join(_HERE, "lzxc.exe")
REGION_WINDOW = 0x20000

# On Windows a console exe launched from a windowed (no-console) app pops a visible
# terminal window per call -- and lzxc.exe runs once PER CHUNK, in parallel, so a save
# flashed HUNDREDS of terminals. CREATE_NO_WINDOW suppresses every one. (capture_output
# redirects the pipes but does NOT stop the window.)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# ---------------------------------------------------------------- console LZX
FRAME = 0x8000                                # XMemCompress output frame = 32 KB


def _lzxc_raw(data):
    """Run XexTool-RE's lzxc.exe on `data` -> its framed XMemCompress output."""
    if not os.path.exists(LZXC):
        raise RuntimeError("lzxc.exe missing next to the lce package")
    with tempfile.TemporaryDirectory() as td:
        i, o = os.path.join(td, "i"), os.path.join(td, "o")
        open(i, "wb").write(data)
        subprocess.run([LZXC, i, o, hex(REGION_WINDOW)], check=True, capture_output=True,
                       creationflags=_NO_WINDOW)
        return open(o, "rb").read()


def _split_frames(xc):
    """Split lzxc's framed output into per-frame compressed byte blocks."""
    frames = []
    off, n = 0, len(xc)
    while off < n:
        if xc[off] == 0xFF:
            comp = struct.unpack_from(">H", xc, off + 3)[0]; off += 5
        else:
            comp = struct.unpack_from(">H", xc, off)[0]; off += 2
        if comp == 0:
            break
        frames.append(xc[off:off + comp]); off += comp
    return frames


def encode_chunk(chunk_nbt):
    """Legacy chunk NBT bytes -> (sector bytes padded to 4096, sector count).

    Segment = [0xFF][u16 rle_len][u16 comp_len][LZX bitstream][5 zero bytes] --
    a single 32 KB frame. The 5-byte trailer is REQUIRED (seglen = comp_len + 10)
    or the console mis-decodes.

    TU0 (Beta 1.6.6) does NOT support multi-frame chunks: a chunk whose RLE
    exceeds 32 KB freezes the console at "Loading spawn area". We refuse to emit
    one -- callers must keep a chunk's content down (fewer tile entities / less
    metadata) so its RLE stays <= 32 KB."""
    rle = codec.rle_encode(chunk_nbt)
    if len(rle) > FRAME:
        raise ValueError(
            "chunk RLE %d > 32KB: TU0 cannot load multi-frame chunks; "
            "reduce chunk content (tile entities / block variety)" % len(rle))
    bits = b"".join(_split_frames(_lzxc_raw(rle)))
    seg = b"\xff" + struct.pack(">H", len(rle)) + struct.pack(">H", len(bits)) + bits + b"\x00" * 5
    body = struct.pack(">I", 0x80000000 | len(seg)) + struct.pack(">I", len(chunk_nbt)) + seg
    body += b"\x00" * ((-len(body)) % 4096)
    return body, len(body) // 4096


# ---------------------------------------------------------------- retail container
def read_retail(path):
    """path = a .bin dir or a savegame.dat -> (ents, {name: bytes}, ver).
    ents preserves VFS order + the 8-byte per-file timestamp."""
    if os.path.isdir(path):
        path = os.path.join(path, "savegame.dat")
    d = open(path, "rb").read()
    blob, meta = recover.decode(d, tolerant=True)
    entry = meta.get("vfs_entry", 144)                  # 144 retail (TU0-current) / 136 legacy
    io_, count, _ = recover.parse_vfs(blob, entry)
    ents = []
    for k in range(count):
        p = io_ + k * entry
        name = blob[p:p + 128].split(b"\x00\x00")[0].decode("utf-16-be", "replace")
        ln = struct.unpack_from(">I", blob, p + 128)[0]
        of = struct.unpack_from(">I", blob, p + 132)[0]
        ts = blob[p + 136:p + 144] if entry >= 144 else b"\x00" * 8
        ents.append([name, of, ln, ts])
    filedata = {e[0]: blob[e[1]:e[1] + e[2]] for e in ents}
    ver = struct.unpack_from(">I", blob, 8)[0] if entry >= 144 else 2
    return ents, filedata, ver


def xmem_compress_native(blob):
    """Compress the whole container with the native lzxc.exe (XexTool-RE
    XMemCompress). ~15-20x faster than the pure-python codec.xmem_compress.

    lzxc frames at 32 KB but leaves the final partial frame with a SHORT header
    (implying a full 0x8000 frame). The console reads per-frame sizes, so the
    final partial frame MUST carry the long [0xFF][u16 raw] header (as real
    console saves do) or the whole save is rejected. We re-frame lzxc's output
    to that convention: full frames -> short header, final partial -> 0xFF.
    Self-checks by decoding; raises on mismatch so callers fall back to python."""
    if not os.path.exists(LZXC):
        raise RuntimeError("lzxc.exe missing")
    frames = _split_frames(_lzxc_raw(blob))
    out = bytearray()
    n = len(frames)
    for k, bits in enumerate(frames):
        raw = FRAME if k < n - 1 else len(blob) - FRAME * (n - 1)
        if raw == FRAME:
            out += struct.pack(">H", len(bits)) + bits               # full frame
        else:
            out += b"\xff" + struct.pack(">H", raw) + struct.pack(">H", len(bits)) + bits
    stream = bytes(out)
    # verify the native stream decodes back to exactly this blob
    probe = struct.pack(">III", 12 + len(stream), 0, len(blob)) + stream
    probe += b"\x00" * ((-len(probe)) % 512)
    back, _ = recover.decode(probe, tolerant=True)
    if back[:len(blob)] != blob:
        raise RuntimeError("native compress round-trip mismatch")
    return stream


def build_retail(ents, filedata, ver=2, fast=True):
    """Rebuild a retail savegame.dat from edited filedata (lossless container).

    fast=True uses the native lzxc.exe container compressor (self-verified, with
    an automatic fall-back to the pure-python compressor if anything is off)."""
    body = bytearray(12)
    laid = []
    for name, _of, _ln, ts in ents:
        data = filedata[name]
        off = len(body); body += data
        laid.append((name, len(data), off, ts))
    index_off = len(body)
    for name, ln, off, ts in laid:
        nb = name.encode("utf-16-be")[:128]
        body += nb + b"\x00" * (128 - len(nb)) + struct.pack(">II", ln, off) + ts
    struct.pack_into(">III", body, 0, index_off, len(laid), ver)
    body = bytes(body)
    stream = None
    if fast:
        try:
            stream = xmem_compress_native(body)
        except Exception:
            stream = None                       # fall back to the proven path
    if stream is None:
        stream = codec.xmem_compress(body)
    datalen = 12 + len(stream)
    full = bytearray(struct.pack(">III", datalen, 0, len(body)) + stream)
    full += b"\x00" * ((-len(full)) % 512)
    return bytes(full)


# ---------------------------------------------------------------- region surgery
def region_replace_chunks(region, updates, on_chunk=None):
    """Replace many chunks in one region pass. updates: {(cx,cz): chunk_nbt}.
    Re-encodes only the given chunks and re-lays the region a single time.
    on_chunk (optional) fires once per re-encoded chunk (from a worker thread) so a
    caller can show live compression progress."""
    ts = region[4096:8192]
    chunks = {}
    for i in range(1024):
        v = struct.unpack_from(">I", region, i * 4)[0]
        sec, cnt = v >> 8, v & 0xFF
        if sec:
            chunks[(i % 32, i // 32)] = region[sec * 4096: sec * 4096 + cnt * 4096]
    if updates:
        # Each chunk is an independent lzxc.exe subprocess (I/O-bound on the process
        # wait), so encode them in parallel -- a whole-world edit re-encodes thousands
        # of chunks and doing them serially is minutes of pure spawn latency.
        items = list(updates.items())
        workers = min(32, max(4, (os.cpu_count() or 4) * 4))

        def _enc(nbt):
            b = encode_chunk(nbt)[0]
            if on_chunk is not None:
                on_chunk()                       # progress tick (thread-safe callback)
            return b
        with ThreadPoolExecutor(max_workers=workers) as ex:
            bodies = list(ex.map(_enc, (n for _k, n in items)))
        for (k, _n), body in zip(items, bodies):
            chunks[k] = body
    loc = bytearray(4096); sectors = bytearray(); sn = 2
    for (cx, cz), data in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        struct.pack_into(">I", loc, (cx + cz * 32) * 4, (sn << 8) | (len(data) // 4096))
        sectors += data; sn += len(data) // 4096
    return bytes(loc) + ts + bytes(sectors)


def region_replace_chunk(region, tcx, tcz, new_chunk_nbt):
    """Replace one chunk's sectors in a region, re-encoding only that chunk."""
    return region_replace_chunks(region, {(tcx, tcz): new_chunk_nbt})


# ---------------------------------------------------------------- NBT builders (nbt engine)
def _entity_base(entity_id, x, y, z):
    c = N.Compound()
    c.set("id", N.STRING, entity_id)
    c.set("Pos", N.LIST, N.List(N.DOUBLE, [float(x), float(y), float(z)]))
    c.set("Motion", N.LIST, N.List(N.DOUBLE, [0.0, 0.0, 0.0]))
    c.set("Rotation", N.LIST, N.List(N.FLOAT, [0.0, 0.0]))
    c.set("FallDistance", N.FLOAT, 0.0)
    c.set("Fire", N.SHORT, -20)
    c.set("Air", N.SHORT, 300)
    c.set("OnGround", N.BYTE, 1)
    return c


def mob_compound(mob_id, x, y, z):
    """A standard Mob save compound (Tag COMPOUND) ready to append to Entities."""
    c = _entity_base(mob_id, x, y, z)
    for k in ("HurtTime", "DeathTime", "AttackTime"):
        c.set(k, N.SHORT, 0)
    return c


def spawner_compound(x, y, z, mob="Giant", delay=20):
    c = N.Compound()
    c.set("id", N.STRING, "MobSpawner")
    c.set("x", N.INT, int(x)); c.set("y", N.INT, int(y)); c.set("z", N.INT, int(z))
    c.set("EntityId", N.STRING, mob)
    c.set("Delay", N.SHORT, delay)
    return c


def item_compound(item_id, count=1, damage=0, slot=0):
    c = N.Compound()
    c.set("id", N.SHORT, item_id)
    c.set("Damage", N.SHORT, damage)
    c.set("Count", N.BYTE, count)
    c.set("Slot", N.BYTE, slot)
    return c
