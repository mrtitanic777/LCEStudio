"""Load an LCE world save into an in-memory, editable block volume.

Block ids for a chunk live in a numpy array indexed [x, z, y] (x,z in 0..15,
y in 0..127) — the classic Alpha/Beta order  index = (x*16 + z)*128 + y."""
import io
import os
import struct
import tempfile
import numpy as np

from .. import codec as lce

CHUNK_X = 16
CHUNK_Z = 16
CHUNK_Y = 256          # full LCE height. Newer (Aquatic/Elytra) worlds put builds in the
                       # upper 128 (surface can sit at y127); old 128-tall chunks pad up.
SRC_SECTION = 128      # one stored block section is 128 tall (old-NBT / compressed half)


# ---- minimal big-endian NBT reader (enough to pull one chunk apart) ---------
class _NBT:
    def __init__(self, d):
        self.d = d
        self.o = 0

    def u1(self):
        v = self.d[self.o]; self.o += 1; return v

    def u2(self):
        v = struct.unpack_from(">H", self.d, self.o)[0]; self.o += 2; return v

    def i4(self):
        v = struct.unpack_from(">i", self.d, self.o)[0]; self.o += 4; return v

    def name(self):
        n = self.u2(); s = self.d[self.o:self.o + n]; self.o += n; return s.decode("latin1")

    def skip(self, t):
        if t == 1: self.o += 1
        elif t == 2: self.o += 2
        elif t in (3, 5): self.o += 4
        elif t in (4, 6): self.o += 8
        elif t == 7: n = self.i4(); self.o += n            # (i4 advances o by 4 FIRST)
        elif t == 8: n = self.u2(); self.o += n
        elif t == 11: n = self.i4(); self.o += 4 * n
        elif t == 12: n = self.i4(); self.o += 8 * n
        elif t == 9:
            et = self.u1(); n = self.i4()
            for _ in range(n): self.skip(et)
        elif t == 10:
            while True:
                tt = self.u1()
                if tt == 0: break
                self.name(); self.skip(tt)
        else:
            raise ValueError("bad tag %d" % t)

    def _read_items(self):
        """A TAG_List of item compounds -> [(id, count)] (id short int or string)."""
        et = self.u1(); count = self.i4()
        items = []
        if et != 10:
            for _ in range(count):
                self.skip(et)
            return items
        for _ in range(count):
            iid = None; cnt = 1
            while True:
                t = self.u1()
                if t == 0:
                    break
                nm = self.name()
                if nm == "id" and t == 2:
                    iid = self.u2()
                elif nm == "id" and t == 8:
                    n = self.u2(); iid = self.d[self.o:self.o + n].decode("latin1", "ignore"); self.o += n
                elif nm == "Count" and t == 1:
                    cnt = self.u1()
                    if cnt > 127:
                        cnt -= 256
                else:
                    self.skip(t)
            if iid is not None:
                items.append((iid, cnt))
        return items

    def _read_ent_list(self, listname, out, cont):
        """A TAG_List of entity/tile-entity compounds -> append (kind, name, x, y, z)
        to `out`; containers with an Items list -> (name, x, y, z, items) to `cont`."""
        et = self.u1(); count = self.i4()
        if et != 10:                                  # not a list of compounds
            for _ in range(count):
                self.skip(et)
            return
        kind = "tile" if listname == "TileEntities" else "mob"
        for _ in range(count):
            eid = None; pos = None; ix = iy = iz = None; items = None
            while True:
                t = self.u1()
                if t == 0:
                    break
                nm = self.name()
                if t == 8 and nm == "id":
                    n = self.u2(); eid = self.d[self.o:self.o + n].decode("latin1", "ignore"); self.o += n
                elif t == 9 and nm == "Pos":
                    pet = self.u1(); pn = self.i4(); vals = []
                    for _ in range(pn):
                        if pet == 6:
                            vals.append(struct.unpack_from(">d", self.d, self.o)[0]); self.o += 8
                        elif pet == 5:
                            vals.append(struct.unpack_from(">f", self.d, self.o)[0]); self.o += 4
                        else:
                            self.skip(pet)
                    if len(vals) >= 3:
                        pos = vals[:3]
                elif t == 9 and nm == "Items":
                    items = self._read_items()
                elif t == 3 and nm == "x": ix = self.i4()
                elif t == 3 and nm == "y": iy = self.i4()
                elif t == 3 and nm == "z": iz = self.i4()
                else:
                    self.skip(t)
            if pos is not None:
                out.append((kind, eid or "?", pos[0], pos[1], pos[2]))
            elif ix is not None and iy is not None and iz is not None:
                out.append((kind, eid or "?", ix + 0.5, iy + 0.5, iz + 0.5))
            if items and ix is not None:
                cont.append((eid or "?", ix, iy, iz, items))

    def scan_entities(self, out, cont):
        """Scan a (Level or root) compound for Entities / TileEntities lists,
        descending into a nested Level compound. Reader must sit just past the
        root compound's tag+name."""
        while True:
            t = self.u1()
            if t == 0:
                return out
            nm = self.name()
            if t == 10 and nm == "Level":
                self.scan_entities(out, cont)         # descend, then continue the parent
            elif t == 9 and nm in ("Entities", "TileEntities"):
                self._read_ent_list(nm, out, cont)
            else:
                self.skip(t)

    def parse_chunk(self):
        """Return (Blocks bytes, Data bytes) from a chunk root. The Level tag order
        is NOT fixed -- these saves store Blocks, LastUpdate, xPos, Data, ... -- so we
        scan (skipping intervening tags) until BOTH Blocks and Data are found, then
        stop before the heavy Entities/TileEntities lists (which come after Data)."""
        t = self.u1(); self.name()          # root compound
        assert t == 10
        tt = self.u1(); self.name()          # -> Level compound
        assert tt == 10
        blocks = data = None
        while True:
            tag = self.u1()
            if tag == 0: break
            nm = self.name()
            if tag == 7 and nm == "Blocks":
                n = self.i4(); blocks = self.d[self.o:self.o + n]; self.o += n
            elif tag == 7 and nm == "Data":
                n = self.i4(); data = self.d[self.o:self.o + n]; self.o += n
            else:
                self.skip(tag)
            if blocks is not None and data is not None:
                break                     # have both -> skip the entity lists
        return blocks, data


def _aquatic_entity_nbt(raw):
    """Find the entity NBT compound inside a raw format-12 (Aquatic) chunk. The
    entities live in a top-level compound holding `Entities`/`TileEntities` lists,
    but format12's `rfind(0a0000)` grabs a LATER empty compound and misses them.
    Scan every `0a 00 00` and return the slice at the first one whose top level
    actually has an Entities/TileEntities list."""
    i = 0
    while True:
        p = raw.find(b"\x0a\x00\x00", i)
        if p < 0:
            return b""
        r = _NBT(raw[p:])
        try:
            t = r.u1(); r.name()                  # root compound
            if t == 10:
                while True:
                    tag = r.u1()
                    if tag == 0:
                        break
                    nm = r.name()
                    if tag == 9 and nm in ("Entities", "TileEntities"):
                        return raw[p:]
                    r.skip(tag)
        except Exception:
            pass
        i = p + 1


def extract_entities(nbt_bytes):
    """Return (entities, containers): entities = [(kind, name, x, y, z)]; containers =
    [(name, x, y, z, [(item_id, count), ...])] for chests/dispensers/furnaces. Works on
    a full old-NBT chunk and a format-12 entity blob. Never raises."""
    if not nbt_bytes:
        return [], []
    r = _NBT(nbt_bytes)
    out, cont = [], []
    try:
        t = r.u1(); r.name()                  # root compound
        if t != 10:
            return [], []
        r.scan_entities(out, cont)
    except Exception:
        pass
    return out, cont


class World:
    def __init__(self):
        self.chunks = {}                      # (cx, cz) -> np.uint8 [16,16,256] block ids
        self.data = {}                        # (cx, cz) -> np.uint8 [16,16,256] metadata (0..15)
        self.entities = []                    # [(kind, name, x, y, z)] mobs/items + tile-entities
        self.containers = []                  # [(name, x, y, z, [(item_id, count)])] chest/furnace loot
        self.spawn = (0, 72, 0)
        self.player_pos = None                # (x, y, z) of the owner's last position
        self.name = "world"
        self.min_cx = self.min_cz = 0
        self.max_cx = self.max_cz = 0
        self.source = None                    # original save path (for export)
        self.edits = {}                       # (x,y,z) -> block id  (edits to write back)
        self.pasted_tes = []                  # [(tile_entities, ox, oy, oz)] to stamp on export

    def block(self, x, y, z):
        if y < 0 or y >= CHUNK_Y:
            return 0
        cx, lx = divmod(x, CHUNK_X)
        cz, lz = divmod(z, CHUNK_Z)
        arr = self.chunks.get((cx, cz))
        return int(arr[lx, lz, y]) if arr is not None else 0

    def meta(self, x, y, z):
        if y < 0 or y >= CHUNK_Y:
            return 0
        cx, lx = divmod(x, CHUNK_X)
        cz, lz = divmod(z, CHUNK_Z)
        d = self.data.get((cx, cz))
        return int(d[lx, lz, y]) if d is not None else 0

    def has_chunk(self, x, z):
        return (x // CHUNK_X, z // CHUNK_Z) in self.chunks

    def set_block(self, x, y, z, bid, meta=0):
        """Edit a block in a generated chunk. Returns the (cx,cz) that changed, or None."""
        if not (0 <= y < CHUNK_Y):
            return None
        cx, lx = divmod(x, CHUNK_X)
        cz, lz = divmod(z, CHUNK_Z)
        arr = self.chunks.get((cx, cz))
        if arr is None:
            return None                        # only edit already-generated chunks
        arr[lx, lz, y] = bid & 0xFF
        d = self.data.get((cx, cz))
        if d is not None:
            d[lx, lz, y] = meta & 0xF
        self.edits[(x, y, z)] = (bid & 0xFF, meta & 0xF)     # keep the colour/variant
        return (cx, cz)

    def export(self, out=None, backup=True):
        """Apply all edits to the real save via LCE Studio's writer (console-exact).
        Bulk apply (grouped by chunk, numpy-scattered) so a big delete saves in seconds
        instead of replaying millions of blocks one at a time."""
        from .. import World as _LW
        lw = _LW.open(self.source)
        applied = lw.apply_edits(self.edits)
        if self.pasted_tes:                                     # carry chests/signs across
            from .. import schematic as S
            for tes, ox, oy, oz in self.pasted_tes:
                S.stamp_tile_entities(lw, tes, ox, oy, oz)
        path = lw.save(out=out or self.source, backup=backup)   # compressor self-verifies
        return path, applied

    @classmethod
    def load(cls, savefile, progress=None):
        """Build the 3D block volume from an LCE save. Blocks are YZX
        (idx = y + z*128 + x*2048), reshaped to [x, z, y].

        Fast path: pull Blocks/Data straight out of each chunk's raw NBT as BYTES
        (np.frombuffer) with a minimal reader, skipping the full NBT-tree build that
        allocates a 32768-int Python list per chunk -- ~2.4x faster to load. Chunks
        the minimal reader can't handle (newer format-12 'Aquatic' paletted chunks)
        trigger a one-time fall back to the full engine so those saves still load.

        `progress` (optional dict) is updated in place -- {phase, done, total} -- so a
        caller loading on a background thread can animate a loading screen."""
        from .. import World as _LW              # the high-level editable world
        from .. import codec
        w = cls()
        w.source = savefile
        if progress is not None:
            progress["phase"] = "opening save"
        lw = _LW.open(savefile)
        sx, sy, sz = lw.get_spawn()
        w.spawn = (sx if sx is not None else 0, sy if sy is not None else 72,
                   sz if sz is not None else 0)
        w.name = lw.level.get_value("LevelName") or "world"
        try:                                  # the owner's last standing position (for 'go to player')
            pos = lw.player.get_value("Pos")
            if pos is not None and len(pos) == 3:
                w.player_pos = (float(pos[0]), float(pos[1]), float(pos[2]))
        except Exception:
            pass
        n = CHUNK_X * CHUNK_Z * CHUNK_Y
        regions = [x for x in lw._filedata if x.endswith(".mcr") and not x.startswith("DIM")]
        if progress is not None:
            progress.update(phase="reading chunks", done=0, total=max(1, len(regions)))

        w.skipped = 0
        w.aquatic = 0
        for ri, nm in enumerate(regions):
            if progress is not None:
                progress["done"] = ri
            rx, rz = (int(v) for v in nm[:-4].split("/")[-1].split(".")[1:3])
            tf = os.path.join(tempfile.mkdtemp(), "r")
            with open(tf, "wb") as f:
                f.write(lw._filedata[nm])
            for lcx, lcz, nbt_bytes in codec.decode_region(tf, full_height=True):
                gx, gz = rx * 32 + lcx, rz * 32 + lcz
                if w._store_old_nbt(gx, gz, nbt_bytes, n):
                    continue
                if w._store_aquatic(gx, gz, nbt_bytes):
                    w.aquatic += 1
                    continue
                w.skipped += 1                     # unknown chunk format -> skip, never crash

        if w.chunks:
            xs = [c[0] for c in w.chunks]; zs = [c[1] for c in w.chunks]
            w.min_cx, w.max_cx = min(xs), max(xs)
            w.min_cz, w.max_cz = min(zs), max(zs)
        if progress is not None:
            progress.update(phase="ready", done=progress.get("total", 1))
        return w

    def _store_old_nbt(self, gx, gz, nbt_bytes, n):
        """Old-NBT / compressed-expanded chunk: pull Blocks/Data straight out of the raw
        NBT. Source chunks are 128 tall (32768 blocks); a 256-tall one is stored as
        [lower-128 | upper-128], each a normal YZX-128 block. Place them into the viewer's
        256-tall [x,z,y] volume (128-tall sources get air above y127). bytearray ->
        WRITABLE so editing works."""
        try:
            bl, da = _NBT(nbt_bytes).parse_chunk()
        except Exception:
            return False
        sb = CHUNK_X * CHUNK_Z * SRC_SECTION              # 32768 blocks per 128-tall section
        if bl is None or len(bl) not in (sb, 2 * sb):
            return False
        arr = np.zeros((CHUNK_X, CHUNK_Z, CHUNK_Y), np.uint8)
        arr[:, :, :SRC_SECTION] = np.frombuffer(bytearray(bl[:sb]), np.uint8).reshape(
            CHUNK_X, CHUNK_Z, SRC_SECTION)
        if len(bl) == 2 * sb:                             # 256-tall: stack the upper section
            arr[:, :, SRC_SECTION:2 * SRC_SECTION] = np.frombuffer(
                bytearray(bl[sb:2 * sb]), np.uint8).reshape(CHUNK_X, CHUNK_Z, SRC_SECTION)
        self.chunks[(gx, gz)] = arr
        if da is not None and len(da) >= sb // 2:
            dz = np.zeros((CHUNK_X, CHUNK_Z, CHUNK_Y), np.uint8)

            def _unpack(nibbytes):                        # nibble bytes -> [x,z,128] values
                db = np.frombuffer(bytes(nibbytes), np.uint8)
                nib = np.empty(db.size * 2, np.uint8)
                nib[0::2] = db & 0x0F; nib[1::2] = db >> 4
                return nib.reshape(CHUNK_X, CHUNK_Z, SRC_SECTION)

            dz[:, :, :SRC_SECTION] = _unpack(da[:sb // 2])
            if len(da) >= sb:                             # upper-section metadata
                dz[:, :, SRC_SECTION:2 * SRC_SECTION] = _unpack(da[sb // 2:sb])
            self.data[(gx, gz)] = dz
        e, c = extract_entities(nbt_bytes); self.entities.extend(e); self.containers.extend(c)
        return True

    def _store_aquatic(self, gx, gz, nbt_bytes):
        """Format-12 ('Aquatic', version 0x0C) paletted chunk. It's 256 tall with 11-bit
        ids; the viewer is now 256 tall too, so we keep the FULL column (the surface of a
        custom-superflat world can sit at y127 with builds stacked above it). Ids > 255
        (Aquatic-only blocks with no classic id) drop to air."""
        from .. import format12
        try:
            dec = format12.decode_chunk(nbt_bytes)
        except Exception:
            return False
        ids = dec["ids"][:, :CHUNK_Y, :]           # [x, y, z] uint16, full 256 height
        md = dec["data"][:, :CHUNK_Y, :]
        ids = np.where(ids > 255, 0, ids).astype(np.uint8)
        self.chunks[(gx, gz)] = np.ascontiguousarray(ids.transpose(0, 2, 1))   # -> [x, z, y]
        self.data[(gx, gz)] = np.ascontiguousarray(md.transpose(0, 2, 1).astype(np.uint8))
        e, c = extract_entities(_aquatic_entity_nbt(nbt_bytes)); self.entities.extend(e); self.containers.extend(c)
        return True


def _read_level_dat(data, w):
    """Pull LevelName + Spawn{X,Y,Z} out of level.dat (big-endian NBT)."""
    p = _NBT(data)
    try:
        t = p.u1(); p.name()                  # root
        tt = p.u1(); p.name()                 # Data
        spawn = {}
        while True:
            tag = p.u1()
            if tag == 0: break
            nm = p.name()
            if tag == 3 and nm in ("SpawnX", "SpawnY", "SpawnZ"):
                spawn[nm] = p.i4()
            elif tag == 8 and nm == "LevelName":
                n = p.u2(); w.name = data[p.o:p.o + n].decode("latin1"); p.o += n
            else:
                p.skip(tag)
        if spawn:
            w.spawn = (spawn.get("SpawnX", 0), spawn.get("SpawnY", 72) + 2, spawn.get("SpawnZ", 0))
    except Exception:
        pass


if __name__ == "__main__":
    import sys, time
    t = time.time()
    w = World.load(sys.argv[1])
    total = sum(int((c != 0).sum()) for c in w.chunks.values())
    print("world:", w.name, "| chunks:", len(w.chunks),
          "| non-air blocks:", total, "| spawn:", w.spawn,
          "| bounds cx[%d..%d] cz[%d..%d]" % (w.min_cx, w.max_cx, w.min_cz, w.max_cz),
          "| %.1fs" % (time.time() - t))
    # sanity: y=0 layer should be mostly bedrock (id 7)
    import collections
    c = collections.Counter()
    for arr in w.chunks.values():
        c.update(arr[:, :, 0].flatten().tolist())
    print("y=0 layer block ids:", dict(c.most_common(4)))
