"""lce_recover.py -- analyze and recover corrupted Minecraft: Xbox 360 (LCE) saves.

A Minecraft 360 save container (an extracted STFS package, here a .zip) holds one
or more world save slots.  Each slot is a folder `Save<timestamp>.bin/` containing
`savegame.dat` -- an LCE world save.  This tool understands two savegame.dat header
layouts and can decode, health-check, and recover them:

  * legacy/tutorial : [u32 ver=0][u32 dec_size][XMemCompress chunks @8]   136-byte VFS
  * retail          : [u32 datalen][u32 0][u32 dec_size][chunks @12]      144-byte VFS

Both wrap the whole world (a mini-VFS of r.*.mcr region files, DIM-1 nether,
level.dat, data/map_*, players/*) in one XMemCompress/LZX stream, padded to a
512-byte boundary with 0xCD.

Corruption modes handled:
  * truncated / desynced compressed stream  -> tolerant decode salvages the prefix
  * lost tail (index at the end)            -> index reconstructed or borrowed
  * a whole slot damaged                    -> recover from a healthy same-world slot

Usage:
    python lce_recover.py analyze <container.zip | savegame.dat>
    python lce_recover.py recover <container.zip> --world "Stampy's lovely world" \
           --out <fixed.zip>            # substitute corrupt slot from healthy sibling
"""
import os
import struct
import sys
import zipfile

from .. import codec as lce

_NS = lce._num_position_slots(0x20000)
_EXTRA, _BASE = lce._build_pos_tables(_NS)


# ---------------------------------------------------------------- format layer
def detect_format(d):
    """Return (name, dec_size, chunk_start, vfs_entry_size)."""
    if struct.unpack_from(">I", d, 0)[0] == 0:
        return ("legacy", struct.unpack_from(">I", d, 4)[0], 8, 136)
    return ("retail", struct.unpack_from(">I", d, 8)[0], 12, 144)


def _read_chunks(d, start):
    """Concatenate the XMemCompress chunk payloads. Returns (payload, truncated)."""
    datalen = struct.unpack_from(">I", d, 0)[0]
    n = len(d)
    payload = bytearray()
    off = start
    truncated = False
    while off < datalen and off < n:
        b = d[off]
        if b == 0xFF:                       # 5-byte long-chunk header
            if off + 5 > n:
                truncated = True; break
            comp = struct.unpack_from(">H", d, off + 3)[0]; off += 5
        else:
            if off + 2 > n:
                truncated = True; break
            comp = struct.unpack_from(">H", d, off)[0]; off += 2
        if comp == 0:                       # terminator
            break
        if off + comp > n:
            truncated = True; break
        payload += d[off:off + comp]; off += comp
    return bytes(payload), truncated


def decode(d, tolerant=True):
    """Decode a savegame.dat -> (blob, meta). meta has format/dec_size/recovered/..."""
    fmt, dec_size, start, entry = detect_format(d)
    payload, truncated = _read_chunks(d, start)
    blob = lce.lzx_decompress_chunk(payload, dec_size, _NS, _EXTRA, _BASE, tolerant=tolerant)
    meta = {"format": fmt, "dec_size": dec_size, "vfs_entry": entry,
            "recovered": len(blob), "truncated": truncated,
            "complete": (len(blob) >= dec_size and not truncated)}
    return blob, meta


def parse_vfs(blob, entry):
    """Parse the mini-VFS index -> (index_off, count, [(name, off, len), ...])."""
    if len(blob) < 8:
        return None, 0, []
    index_off = struct.unpack_from(">I", blob, 0)[0]
    count = struct.unpack_from(">I", blob, 4)[0] if entry == 144 else None
    files = []
    p = index_off
    i = 0
    while p + entry <= len(blob):
        if count is not None and i >= count:
            break
        raw = blob[p:p + 128]
        zp = raw.find(b"\x00\x00")
        name = raw[:zp if zp >= 0 else 128].decode("utf-16-be", "replace")
        ln = struct.unpack_from(">I", blob, p + 128)[0]
        of = struct.unpack_from(">I", blob, p + 132)[0]
        if count is None and not name and ln == 0 and of == 0:
            break
        files.append((name, of, ln))
        p += entry; i += 1
    return index_off, count, files


def health(d):
    """Full health report for one savegame.dat (dict)."""
    blob, meta = decode(d, tolerant=True)
    index_ok = meta["recovered"] >= 8 and meta["complete"]
    io_, count, files = parse_vfs(blob, meta["vfs_entry"]) if index_ok else (None, None, [])
    rec = meta["recovered"]
    graded = []
    for nm, of, ln in files:
        st = "ok" if (nm and 0 <= of and of + ln <= rec) else ("partial" if of < rec else "lost")
        graded.append((nm, of, ln, st))
    meta.update(file_size=len(d), index_off=io_, count=count, files=graded)
    meta["healthy"] = meta["complete"] and bool(files) and all(f[3] == "ok" for f in graded)
    return meta


# ------------------------------------------------------------- container layer
def parse_saveinfo(data):
    """_MinecraftSaveInfo -> {bin_filename: display_name}. Records are 308 bytes:
    256-byte UTF-16BE display name + 52-byte ASCII '.bin' filename."""
    mapping = {}
    if len(data) < 4:
        return mapping
    count = struct.unpack_from(">I", data, 0)[0]
    for i in range(count):
        rec = 4 + i * 308
        if rec + 308 > len(data):
            break
        nm = data[rec:rec + 256]
        zp = nm.find(b"\x00\x00")
        name = nm[:zp if zp >= 0 else 256].decode("utf-16-be", "replace")
        fn = data[rec + 256:rec + 308].split(b"\x00")[0].decode("latin1")
        if fn:
            mapping[fn] = name
    return mapping


def _slot_of(member):
    """'584111F7/00000001/Save....bin/savegame.dat' -> 'Save....bin'."""
    parts = member.split("/")
    for p in parts:
        if p.endswith(".bin"):
            return p
    return parts[-2] if len(parts) >= 2 else member


def _container_files(path):
    """Return (saveinfo_bytes|None, [(member_name, bytes), ...]) for every
    savegame.dat. Works for a .zip container OR an extracted save directory."""
    saveinfo = None
    saves = []
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, path).replace("\\", "/")
                if f == "_MinecraftSaveInfo" and os.path.basename(root) == "_MinecraftSaveInfo":
                    saveinfo = open(full, "rb").read()
                elif f == "savegame.dat":
                    saves.append((rel, open(full, "rb").read()))
    else:
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.endswith("_MinecraftSaveInfo") and not n.endswith(".header"):
                saveinfo = z.read(n)
            elif n.endswith("savegame.dat"):
                saves.append((n, z.read(n)))
    return saveinfo, saves


def scan_container(path):
    """Analyze every savegame.dat in a container (zip or dir). Returns (saveinfo, slots)."""
    saveinfo_bytes, saves = _container_files(path)
    saveinfo = parse_saveinfo(saveinfo_bytes) if saveinfo_bytes else {}
    slots = []
    for name, data in saves:
        slot = _slot_of(name)
        try:
            h = health(data)
        except Exception as e:
            h = {"error": repr(e), "healthy": False}
        h["member"] = name
        h["slot"] = slot
        h["world"] = saveinfo.get(slot, saveinfo.get(slot + ".bin", "?"))
        slots.append(h)
    return saveinfo, slots


# -------------------------------------------------------------------- reporting
def _fmt_size(n):
    for u in ("B", "KB", "MB"):
        if n < 1024 or u == "MB":
            return "%.0f%s" % (n, u) if u == "B" else "%.1f%s" % (n, u)
        n /= 1024.0


def print_report(zip_path):
    saveinfo, slots = scan_container(zip_path)
    print("Container:", zip_path)
    print("Worlds in _MinecraftSaveInfo:")
    for fn, world in saveinfo.items():
        print("   %-26s -> %r" % (fn, world))
    print()
    for h in slots:
        if "error" in h and "format" not in h:
            print("[ERR ] %-24s %r -> %s" % (h["slot"], h.get("world"), h["error"]))
            continue
        tag = "HEALTHY" if h["healthy"] else "CORRUPT"
        pct = 100.0 * h["recovered"] / h["dec_size"] if h["dec_size"] else 0
        print("[%s] %-24s world=%r  fmt=%s  decoded %.1f%% (%d/%d)  files=%d"
              % (tag, h["slot"], h["world"], h["format"], pct,
                 h["recovered"], h["dec_size"], len(h["files"])))
        if not h["healthy"]:
            lost = [f[0] for f in h["files"] if f[3] != "ok"]
            if lost:
                print("        damaged/lost files:", ", ".join(x or "?" for x in lost[:8]))
            elif not h["files"]:
                print("        index unreadable (compressed stream damaged early)")
    return saveinfo, slots


def recommend(slots):
    """Pair each corrupt slot with a healthy same-world source, newest first."""
    healthy = [h for h in slots if h.get("healthy")]
    plans = []
    for h in slots:
        if h.get("healthy"):
            continue
        cand = [s for s in healthy if s["world"] == h["world"] and s["world"] != "?"]
        cand.sort(key=lambda s: s["slot"], reverse=True)     # newest timestamp first
        plans.append((h, cand[0] if cand else None))
    return plans


# --------------------------------------------------------------------- recovery
def recover_zip(zip_path, out_path, substitutions):
    """Write a new container zip with corrupt slots repaired from a healthy source
    slot's bytes. substitutions: {corrupt_slot: source_savegame_bytes}. Both the
    slot's `savegame.dat` and any `savegame.Id_*.dat` shadow copy are replaced, so
    a damaged secondary (e.g. one that starts with 'MZ') can't re-corrupt the load."""
    zin = zipfile.ZipFile(zip_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            slot = _slot_of(item.filename)
            base = item.filename.rsplit("/", 1)[-1]
            if slot in substitutions and (base == "savegame.dat"
                                          or base.startswith("savegame.Id_")):
                data = substitutions[slot]
            zout.writestr(item, data)
    return out_path


def main(argv):
    if len(argv) < 2 or argv[0] not in ("analyze", "recover"):
        print(__doc__); return 1
    path = argv[1]
    if argv[0] == "analyze":
        if path.lower().endswith(".zip") or os.path.isdir(path):
            saveinfo, slots = print_report(path)
            plans = recommend(slots)
            if plans:
                print("\nRecovery options:")
                for corrupt, src in plans:
                    if src:
                        print("   %r: substitute %s  <-  healthy %s"
                              % (corrupt["world"], corrupt["slot"], src["slot"]))
                    else:
                        print("   %r: %s corrupt, NO healthy same-world source found"
                              % (corrupt["world"], corrupt["slot"]))
        else:
            h = health(open(path, "rb").read())
            print("%s: format=%s healthy=%s decoded=%d/%d files=%d"
                  % (path, h["format"], h["healthy"], h["recovered"], h["dec_size"], len(h["files"])))
            for nm, of, ln, st in h["files"]:
                print("   [%-7s] %-24s off=%d len=%d" % (st, nm, of, ln))
        return 0
    if argv[0] == "recover":
        world = None; out = None
        for i, a in enumerate(argv):
            if a == "--world":
                world = argv[i + 1]
            elif a == "--out":
                out = argv[i + 1]
        saveinfo, slots = scan_container(path)
        plans = [(c, s) for (c, s) in recommend(slots)
                 if (world is None or c["world"] == world)]
        subs = {}
        zin = zipfile.ZipFile(path)
        for corrupt, src in plans:
            if not src:
                print("SKIP %r: no healthy source" % corrupt["world"]); continue
            subs[corrupt["slot"]] = zin.read(src["member"])
            print("PLAN %r: %s  <-  %s" % (corrupt["world"], corrupt["slot"], src["slot"]))
        if not subs:
            print("Nothing to recover."); return 1
        if not out:
            out = os.path.splitext(path)[0] + "_recovered.zip"
        recover_zip(path, out, subs)
        print("Wrote", out)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
