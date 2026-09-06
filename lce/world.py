"""High-level editable World model for an LCE retail save.

    w = World.open("Save....bin")
    w.set_spawn(0, 80, 0)
    w.inventory_add(264, 64)                 # 64 diamonds
    w.set_block(10, 64, 10, 41)              # gold block
    w.fill(-5, 63, -5, 5, 63, 5, 20)         # glass floor
    w.add_mob("Giant", -84, 64, 58)
    w.save(backup=True)                      # console-exact, verified

Everything is parsed into the nbt engine, edited as objects, and only dirty
chunks / files are re-encoded on save.
"""
import os
import re
import shutil
import struct
import tempfile

from . import inject, codec, recover
from . import nbt as N


def codec_recover_decode(data):
    return recover.decode(data, tolerant=True)


def _reparse_vfs(blob):
    return recover.parse_vfs(blob, 144)


# tile-entity id -> the block id(s) it may legally sit on. A tile-entity on any other
# block is an ORPHAN (leftover data whose block was replaced) -- invisible, passes every
# file check, and hard-freezes the game when the chunk loads. Types not listed here are
# left alone (unknown -> never removed).
_TE_BLOCKS = {
    "Chest": {54, 146}, "Sign": {63, 68}, "Furnace": {61, 62}, "MobSpawner": {52},
    "RecordPlayer": {84}, "Trap": {23}, "Dropper": {158}, "Music": {25},
    "Cauldron": {118}, "EnchantTable": {116}, "Airportal": {119, 120},
    "Skull": {144}, "FlowerPot": {140}, "Beacon": {138}, "Hopper": {154},
    "Comparator": {149, 150}, "DLDetector": {151, 178}, "Banner": {176, 177},
    "Piston": {29, 33, 34}, "Control": {137}, "EndGateway": {209}, "Bed": {26},
}


def _te_expected_blocks(tid):
    """Block-id set a tile-entity type belongs on, or None if unknown. Accepts both the
    classic ids ('Chest') and flattened Aquatic ids ('minecraft:chest')."""
    if not tid:
        return None
    key = str(tid).split(":")[-1]
    return _TE_BLOCKS.get(key) or _TE_BLOCKS.get(key.title())


def _te_orphaned(tid, bid):
    """True if a tile-entity of type `tid` cannot legally exist on block `bid`."""
    exp = _te_expected_blocks(tid)
    return exp is not None and bid not in exp


def _entity_broken(en):
    """True if an entity has a NaN / infinite / absurd position or motion -- the kind
    that crashes the engine the moment it ticks."""
    import math
    for key, lim in (("Pos", 3.0e7), ("Motion", 1.0e4)):
        vec = en.get_value(key)
        if not vec:
            continue
        for c in vec:
            try:
                c = float(c)
            except Exception:
                return True
            if c != c or math.isinf(c) or abs(c) > lim:
                return True
    return False


# ------------------------------------------------------------------ Chunk
class Chunk:
    """One 16x16x128 chunk. Blocks are YZX: idx = y + z*128 + x*2048."""

    def __init__(self, region, lcx, lcz, root_name, root):
        self.region = region
        self.lcx, self.lcz = lcx, lcz
        self.root_name = root_name
        self.root = root                       # Compound with "Level"
        self.level = root.get_value("Level")   # Compound
        self.dirty = False

    def _mark(self):
        self.dirty = True
        self.region.dirty.add((self.lcx, self.lcz))

    # -- blocks -------------------------------------------------------------
    def _blocks(self):
        return self.level.get_tag("Blocks").value      # list[int] len 32768

    def _data(self):
        t = self.level.get_tag("Data")
        return t.value if t else None

    @staticmethod
    def _idx(x, y, z):
        return y + (z & 15) * 128 + (x & 15) * 2048

    def get_block(self, x, y, z):
        return self._blocks()[self._idx(x, y, z)]

    def set_block(self, x, y, z, bid, data=0):
        i = self._idx(x, y, z)
        self._blocks()[i] = bid & 0xFF
        d = self._data()
        if d is not None:
            ni = i >> 1
            if i & 1:
                d[ni] = (d[ni] & 0x0F) | ((data & 0xF) << 4)
            else:
                d[ni] = (d[ni] & 0xF0) | (data & 0xF)
        self._drop_orphan_te(x, y, z, bid & 0xFF)   # keep tile-entities in sync with the block
        self._mark()

    def _te_positions(self):
        """Cached set of (x,y,z) that hold a tile-entity -> O(1) orphan check in
        set_block. Rebuilt only when the tile-entity count changes, so a bulk fill of
        millions of blocks never re-scans the tile-entity list per block (that turned a
        big delete into a minutes-long hang)."""
        lst = self.tile_entities
        items = lst.items if lst else ()
        cache = getattr(self, "_te_pos_cache", None)
        if cache is None or cache[0] != len(items):
            pos = {(te.get_value("x"), te.get_value("y"), te.get_value("z")) for te in items}
            cache = (len(items), pos)
            self._te_pos_cache = cache
        return cache[1]

    def _drop_orphan_te(self, x, y, z, new_bid):
        """When a block is overwritten, remove any tile-entity at that spot the new block
        no longer supports (e.g. stone placed over a chest). The game does this itself on
        a normal break; an external editor must do it explicitly or it leaves an orphan
        that hard-freezes the game on load."""
        if (x, y, z) not in self._te_positions():    # O(1): the vast majority of blocks
            return
        lst = self.tile_entities
        kept = [te for te in lst.items
                if not ((te.get_value("x"), te.get_value("y"), te.get_value("z")) == (x, y, z)
                        and _te_orphaned(te.get_value("id"), new_bid))]
        if len(kept) != len(lst.items):
            lst.items[:] = kept
            self._te_pos_cache = None                 # count changed -> rebuild next time

    def top_solid(self, x, z, veg=frozenset({0, 6, 17, 18, 30, 31, 37, 38, 39,
                                              40, 50, 63, 64, 65, 66, 78, 83})):
        bl = self._blocks()
        lx, lz = x & 15, z & 15
        for y in range(120, 0, -1):
            b = bl[y + lz * 128 + lx * 2048]
            if b != 0 and b not in veg:
                return y
        return 0

    # -- entities / tile entities ------------------------------------------
    def _list(self, name):
        t = self.level.get_tag(name)
        return t.value if t else None

    @property
    def entities(self):
        return self._list("Entities")

    @property
    def tile_entities(self):
        return self._list("TileEntities")

    def add_entity(self, compound):
        lst = self.entities
        if lst is None:
            lst = N.List(N.COMPOUND, [])
            self.level.set("Entities", N.LIST, lst)
        lst.etype = N.COMPOUND
        lst.items.append(compound)
        self._mark()

    def add_tile_entity(self, compound):
        lst = self.tile_entities
        if lst is None:
            lst = N.List(N.COMPOUND, [])
            self.level.set("TileEntities", N.LIST, lst)
        lst.etype = N.COMPOUND
        lst.items.append(compound)
        self._mark()

    def serialize(self):
        return N.serialize(self.root_name, self.root, N.COMPOUND)


# ------------------------------------------------------------------ Region
class Region:
    def __init__(self, world, name):
        self.world = world
        self.name = name                       # e.g. "r.-1.0.mcr"
        self.rx, self.rz = self._parse_name(name)
        self._raw = world._filedata[name]
        self.chunks = {}                       # (lcx,lcz) -> Chunk (old-NBT), decoded lazily
        self.aquatic_raw = {}                  # (lcx,lcz) -> raw format-12 chunk bytes
        self.aquatic_edits = {}                # (lcx,lcz) -> {(lx,y,lz): (id, meta)}
        self.dirty = set()
        self._offsets = self._index()          # (lcx,lcz) -> byte offset of its sector
        self._decoded = set()                  # coords already decode-attempted
        self._all = False

    @staticmethod
    def _parse_name(name):
        base = name[:-4] if name.endswith(".mcr") else name
        base = base.split("/")[-1]
        parts = base.split(".")
        return int(parts[1]), int(parts[2])

    def _index(self):
        """Parse the .mcr 4 KB header (1024 u32 location entries) -> {(lcx,lcz): offset}.
        Cheap; the expensive LZX decompress is deferred to first access of each chunk."""
        off = {}
        d = self._raw
        for i in range(1024):
            sec = struct.unpack_from(">I", d, i * 4)[0] >> 8
            if sec:
                off[(i % 32, i // 32)] = sec * 4096
        return off

    def _decode_one(self, lcx, lcz):
        """Decompress + parse ONE chunk (idempotent). Touching a few chunks to edit no
        longer decodes the whole ~1000-chunk region -- a big delete was 'forever' because
        of exactly that."""
        if (lcx, lcz) in self._decoded:
            return
        self._decoded.add((lcx, lcz))
        off = self._offsets.get((lcx, lcz))
        if off is None:
            return
        from . import format12
        try:
            nbt_bytes, _ = codec.decode_region_chunk(self._raw[off:])
        except Exception:
            return
        try:                                     # old-NBT first
            name, tag, _ = N.parse_tag(nbt_bytes)
            ch = Chunk(self, lcx, lcz, name, tag.value)
            if ch.level is not None:
                self.chunks[(lcx, lcz)] = ch
                return
        except Exception:
            pass
        try:                                     # else keep raw format-12 (Aquatic)
            if format12.chunk_version(nbt_bytes) == format12.VERSION_AQUATIC:
                self.aquatic_raw[(lcx, lcz)] = nbt_bytes
        except Exception:
            pass

    def decode_all(self):
        """Force-decode every present chunk -- for whole-region scans (downgrade / repair
        / relight / map). Editing paths use chunk()/has_aquatic() and decode on demand."""
        if not self._all:
            for coord in self._offsets:
                self._decode_one(*coord)
            self._all = True

    def chunk(self, lcx, lcz):
        if (lcx, lcz) not in self._decoded:
            self._decode_one(lcx, lcz)
        return self.chunks.get((lcx, lcz))

    def has_aquatic(self, lcx, lcz):
        if (lcx, lcz) not in self._decoded:
            self._decode_one(lcx, lcz)
        return (lcx, lcz) in self.aquatic_raw

    def edit_aquatic(self, lcx, lcz, lx, y, lz, bid, meta):
        """Record a block edit against a raw format-12 chunk (applied at rebuild)."""
        self.aquatic_edits.setdefault((lcx, lcz), {})[(lx, y, lz)] = (bid & 0xFF, meta & 0xF)

    def rebuild(self, on_chunk=None):
        """Return updated region bytes, re-encoding dirty old-NBT chunks AND any
        edited format-12 (Aquatic) chunks (applying edits onto their original grid).
        on_chunk (optional) is called once per re-encoded chunk, for progress."""
        updates = {(lcx, lcz): self.chunks[(lcx, lcz)].serialize()
                   for (lcx, lcz) in self.dirty}
        if self.aquatic_edits:
            from . import format12
            for (lcx, lcz), edits in self.aquatic_edits.items():
                original = self.aquatic_raw.get((lcx, lcz))
                if original is not None:
                    updates[(lcx, lcz)] = format12.encode_chunk(original, edits)
        self._raw = inject.region_replace_chunks(self._raw, updates, on_chunk=on_chunk)
        self.dirty.clear(); self.aquatic_edits.clear()
        return self._raw


# ------------------------------------------------------------------ World
class World:
    def __init__(self, path, owner=None):
        self.path = path
        self._ents, self._filedata, self._ver = inject.read_retail(path)
        self._regions = {}
        # level.dat + player parsed as nbt
        self._level_root_name, ltag, _ = N.parse_tag(self._filedata["level.dat"])
        self._level_root = ltag.value
        self.level = self._find_data(self._level_root)      # compound with SpawnX etc.
        self._pkey = self._select_player(owner)
        self._player_root_name, ptag, _ = N.parse_tag(self._filedata[self._pkey])
        self.player = ptag.value
        self._dirty_level = False
        self._dirty_player = False

    def _select_player(self, owner=None):
        """Pick the OWNER's player file, not just the first one. An LCE world can
        hold many `players/<XUID>.dat` (every profile / split-screen guest that ever
        played it -- common in downloaded maps like Stampy's Lovely World).

        The owner is identified ONLY by the profile id carried in the save itself
        (the STFS/CON package header, passed in as `owner`). The filesystem path is
        deliberately NOT consulted -- where a Nexia-library folder happens to sit is
        an external arrangement, not part of the save. A bare extracted savegame.dat
        has no owner field, so without an explicit `owner` we cannot know which
        profile is 'you' and fall back to the first entry."""
        players = [n for n in self._filedata if n.startswith("players/")]
        if not players:
            raise KeyError("save has no players/ entries")
        if owner is not None:                               # a profile id may arrive as
            cands = [str(owner).strip()]                    # int, decimal str, or 16-hex str
            if re.fullmatch(r"[0-9A-Fa-f]{16}", cands[0]):  # hex XUID -> decimal filename
                cands.append(str(int(cands[0], 16)))
            for c in cands:
                key = "players/%s.dat" % c
                if key in self._filedata:
                    return key
        return players[0]                                   # no owner in the save -> arbitrary

    @classmethod
    def open(cls, path, owner=None):
        """Open a save. `path` may be an extracted savegame.dat (or its folder), OR a
        raw STFS `CON ` package straight off the console -- in that case it is unpacked
        to a temp dir and its header profile id is used as the owner (so the right
        player is selected) unless an explicit `owner` was given."""
        from . import stfs
        if os.path.isfile(path) and stfs.is_con(path):
            tmp = tempfile.mkdtemp()
            info = stfs.unpack_con(path, tmp)
            dat = os.path.join(tmp, "savegame.dat")
            w = cls(dat, owner=(owner if owner is not None else info.get("profile")))
            w._con_src = path                    # remember we came from a CON package
            return w
        return cls(path, owner=owner)

    @staticmethod
    def _find_data(root):
        """LCE level.dat may hold the fields directly or under a child compound."""
        if root.get_tag("SpawnX") is not None:
            return root
        for _n, t in root.items():
            if t.id == N.COMPOUND and t.value.get_tag("SpawnX") is not None:
                return t.value
        return root

    # -- level --------------------------------------------------------------
    def get_spawn(self):
        return (self.level.get_value("SpawnX"), self.level.get_value("SpawnY"),
                self.level.get_value("SpawnZ"))

    def set_spawn(self, x, y, z):
        for k, v in (("SpawnX", x), ("SpawnY", y), ("SpawnZ", z)):
            if self.level.get_tag(k) is not None:
                self.level.set_value(k, int(v))
            else:
                self.level.set(k, N.INT, int(v))
        self._dirty_level = True

    def set_level(self, key, value, tid=None):
        """Set any level.dat field; tid inferred from existing tag if omitted."""
        t = self.level.get_tag(key)
        if t is not None and tid is None:
            t.value = value
        else:
            self.level.set(key, tid if tid is not None else N._tid_of(value), value)
        self._dirty_level = True

    # -- player / inventory -------------------------------------------------
    @property
    def _inventory(self):
        t = self.player.get_tag("Inventory")
        if t is None:
            self.player.set("Inventory", N.LIST, N.List(N.COMPOUND, []))
            t = self.player.get_tag("Inventory")
        return t.value

    def inventory(self):
        """List of dicts describing current inventory."""
        return [{"id": it.get_value("id"), "Count": it.get_value("Count"),
                 "Damage": it.get_value("Damage"), "Slot": it.get_value("Slot")}
                for it in self._inventory]

    def inventory_add(self, item_id, count=1, damage=0, slot=None):
        inv = self._inventory
        inv.etype = N.COMPOUND
        used = {it.get_value("Slot") for it in inv}
        if slot is None:
            slot = next(s for s in range(0, 36) if s not in used)
        inv.items.append(inject.item_compound(item_id, count, damage, slot))
        self._dirty_player = True
        return slot

    def inventory_clear(self):
        self._inventory.items.clear()

    # -- player stats / profile switching ----------------------------------
    def players(self):
        """All profile keys in the save ('players/<XUID>.dat')."""
        return sorted(n for n in self._filedata if n.startswith("players/"))

    def switch_player(self, pkey):
        """Make `pkey` the active player; subsequent inventory/stat edits target it."""
        if pkey not in self._filedata:
            raise KeyError(pkey)
        self._pkey = pkey
        self._player_root_name, ptag, _ = N.parse_tag(self._filedata[pkey])
        self.player = ptag.value

    def set_player_stat(self, name, value):
        """Set a numeric player tag (Health, foodLevel, XpLevel, Air, ...), keeping
        its existing NBT type; adds it (short, or int for the known int stats)."""
        t = self.player.get_tag(name)
        if t is not None:
            t.value = value
        else:
            tid = N.INT if name in ("XpLevel", "XpTotal", "foodLevel") else N.SHORT
            self.player.set(name, tid, value)
        self._dirty_player = True
        self._dirty_player = True

    def get_player_pos(self):
        t = self.player.get_tag("Pos")
        return list(t.value.items) if t else None

    def set_player_pos(self, x, y, z):
        self.player.set("Pos", N.LIST, N.List(N.DOUBLE, [float(x), float(y), float(z)]))
        self._dirty_player = True

    def set_player(self, key, value, tid=None):
        t = self.player.get_tag(key)
        if t is not None and tid is None:
            t.value = value
        else:
            self.player.set(key, tid if tid is not None else N._tid_of(value), value)
        self._dirty_player = True

    # -- profile management (players/<XUID>.dat) ---------------------------
    @staticmethod
    def player_xuid(pkey):
        """'players/2533274.dat' -> '2533274'."""
        return pkey[len("players/"):-4] if pkey.startswith("players/") else pkey

    def _player_nbt(self, pkey):
        nm, tag, _ = N.parse_tag(self._filedata[pkey])
        return nm, tag.value

    def _ent_ts(self, name):
        """The 8-byte VFS timestamp of an entry (for cloning into a new one)."""
        for e in self._ents:
            if e[0] == name:
                return e[3]
        return b"\x00" * 8

    def _write_player_nbt(self, pkey, nm, comp):
        self._filedata[pkey] = N.serialize(nm, comp, N.COMPOUND)
        if pkey == self._pkey:                          # keep the active-player view in sync
            self.player = comp
            self._player_root_name = nm

    def player_info(self, pkey):
        """A summary dict for one profile (safe to call on any profile, not just active)."""
        _nm, p = self._player_nbt(pkey)
        pos = p.get_value("Pos")
        inv = p.get_value("Inventory") or []
        return {
            "key": pkey, "xuid": self.player_xuid(pkey), "active": pkey == self._pkey,
            "pos": [round(float(v), 1) for v in pos] if pos else None,
            "dimension": p.get_value("Dimension"), "health": p.get_value("Health"),
            "food": p.get_value("foodLevel"), "xp": p.get_value("XpLevel"),
            "gametype": p.get_value("playerGameType"),
            "spawn": (p.get_value("SpawnX"), p.get_value("SpawnY"), p.get_value("SpawnZ")),
            "items": len(inv),
        }

    def list_players(self):
        return [self.player_info(pk) for pk in self.players()]

    def add_player(self, xuid, from_pkey=None):
        """Create players/<xuid>.dat, cloned from `from_pkey` (or the active player) so the
        NBT is valid. Returns the new key. Edit it afterwards (pos/spawn/inventory)."""
        key = "players/%s.dat" % str(xuid).strip()
        if key in self._filedata:
            raise ValueError("profile %s already exists" % xuid)
        src = from_pkey or self._pkey
        self._filedata[key] = self._filedata[src]       # bytes are immutable -> safe clone
        self._ents.append([key, 0, 0, self._ent_ts(src)])
        return key

    def remove_player(self, pkey):
        """Delete a profile. Refuses the last remaining one; re-points the active player."""
        if pkey not in self._filedata:
            raise KeyError(pkey)
        if len(self.players()) <= 1:
            raise ValueError("cannot remove the only remaining player profile")
        del self._filedata[pkey]
        self._ents[:] = [e for e in self._ents if e[0] != pkey]
        if pkey == self._pkey:
            self.switch_player(self.players()[0])

    def rename_player(self, pkey, new_xuid):
        """Reassign a profile to a different account by changing its XUID (the filename)."""
        new_key = "players/%s.dat" % str(new_xuid).strip()
        if new_key == pkey:
            return new_key
        if new_key in self._filedata:
            raise ValueError("profile %s already exists" % new_xuid)
        self._filedata[new_key] = self._filedata.pop(pkey)
        for e in self._ents:
            if e[0] == pkey:
                e[0] = new_key
        if pkey == self._pkey:
            self._pkey = new_key
        return new_key

    def edit_player(self, pkey, pos=None, spawn=None, health=None, food=None,
                    xp=None, gametype=None, dimension=None, clear_inventory=False):
        """Edit ONE profile in place (need not be the active one). Only the given fields
        change; Motion/FallDistance are zeroed with a new Pos so it doesn't load mid-fall."""
        nm, p = self._player_nbt(pkey)
        if pos is not None:
            p.set("Pos", N.LIST, N.List(N.DOUBLE, [float(pos[0]), float(pos[1]), float(pos[2])]))
            p.set("Motion", N.LIST, N.List(N.DOUBLE, [0.0, 0.0, 0.0]))
            p.set("FallDistance", N.FLOAT, 0.0)
        if spawn is not None:
            for k, v in zip(("SpawnX", "SpawnY", "SpawnZ"), spawn):
                p.set(k, N.INT, int(v))
        if health is not None:
            p.set("Health", N.SHORT, int(health))
        if food is not None:
            p.set("foodLevel", N.INT, int(food))
        if xp is not None:
            p.set("XpLevel", N.INT, int(xp))
        if gametype is not None:
            p.set("playerGameType", N.INT, int(gametype))
        if dimension is not None:
            p.set("Dimension", N.INT, int(dimension))
        if clear_inventory:
            inv = p.get_tag("Inventory")
            if inv is not None:
                inv.value.items[:] = []
        self._write_player_nbt(pkey, nm, p)

    def import_player(self, src, src_pkey=None, new_xuid=None):
        """Copy a profile from another save (path or open World) into this one."""
        other = src if isinstance(src, World) else World.open(src)
        spk = src_pkey or other._pkey
        xuid = str(new_xuid).strip() if new_xuid else other.player_xuid(spk)
        key = "players/%s.dat" % xuid
        if key in self._filedata:
            raise ValueError("profile %s already exists here" % xuid)
        self._filedata[key] = other._filedata[spk]
        self._ents.append([key, 0, 0, other._ent_ts(spk)])
        return key

    # -- regions / chunks / blocks -----------------------------------------
    def region(self, rx, rz, nether=False):
        name = "%sr.%d.%d.mcr" % ("DIM-1" if nether else "", rx, rz)
        if name not in self._filedata:
            return None
        if name not in self._regions:
            self._regions[name] = Region(self, name)
        return self._regions[name]

    def chunk(self, wcx, wcz, nether=False):
        rx, rz = wcx >> 5, wcz >> 5
        reg = self.region(rx, rz, nether)
        return reg.chunk(wcx - rx * 32, wcz - rz * 32) if reg else None

    def get_block(self, x, y, z, nether=False):
        c = self.chunk(x >> 4, z >> 4, nether)
        return c.get_block(x, y, z) if c else None

    def set_block(self, x, y, z, bid, data=0, nether=False):
        """Set a block. Returns the (wcx,wcz) chunk it edited on success, or None if
        that chunk isn't present in the save (consistent with view3d World)."""
        wcx, wcz = x >> 4, z >> 4
        c = self.chunk(wcx, wcz, nether)
        if c is not None:
            c.set_block(x, y, z, bid, data); return (wcx, wcz)
        # format-12 (Aquatic) chunk -> record the edit for grid re-encode at save
        rx, rz = wcx >> 5, wcz >> 5
        reg = self.region(rx, rz, nether)
        if reg is not None and reg.has_aquatic(wcx - rx * 32, wcz - rz * 32):
            reg.edit_aquatic(wcx - rx * 32, wcz - rz * 32, x & 15, y, z & 15, bid, data)
            return (wcx, wcz)
        return None

    def _apply_chunk_edits(self, ch, idx, bval, mval):
        """Scatter block+meta edits into one chunk's arrays with numpy (fast), then a
        SINGLE tile-entity orphan sweep for the chunk. idx/bval/mval are numpy arrays."""
        import numpy as np
        bl = ch._blocks()
        arr = np.frombuffer(bytes(bytearray(bl)), np.uint8).copy()
        arr[idx] = bval
        bl[:] = arr.tolist()
        d = ch._data()
        if d is not None:
            db = np.frombuffer(bytes(bytearray(d)), np.uint8)
            nib = np.empty(db.size * 2, np.uint8)    # 16384->32768 (128) or 32768->65536 (256)
            nib[0::2] = db & 0x0F; nib[1::2] = db >> 4
            nib[idx] = mval                          # idx < 32768 -> lower section
            d[:] = (nib[0::2] | (nib[1::2] << 4)).astype(np.uint8).tolist()
        tes = ch.tile_entities                       # one orphan sweep, not per block
        if tes and tes.items:
            kept = [te for te in tes.items
                    if not _te_orphaned(te.get_value("id"),
                                        ch.get_block(te.get_value("x"), te.get_value("y"), te.get_value("z")))]
            if len(kept) != len(tes.items):
                tes.items[:] = kept; ch._te_pos_cache = None
        ch._mark()

    def apply_edits(self, edits, nether=False):
        """Apply a dict {(x,y,z): (bid,meta)} (or {(x,y,z): bid}) in BULK -- grouped by
        chunk, numpy-scattered, one tile-entity sweep per chunk. This is what the 3D
        viewer's save uses: replaying millions of edits one-by-one through set_block was
        the reason a big delete took 'forever'. Returns the count applied."""
        import numpy as np
        by_chunk = {}
        for (x, y, z), bm in edits.items():
            if not (0 <= y < 128):
                continue
            bid, meta = bm if isinstance(bm, (tuple, list)) else (bm, 0)
            by_chunk.setdefault((x >> 4, z >> 4), []).append(
                (y + (z & 15) * 128 + (x & 15) * 2048, bid & 0xFF, meta & 0xF))
        applied = 0
        for (wcx, wcz), lst in by_chunk.items():
            ch = self.chunk(wcx, wcz, nether)
            if ch is None:
                continue
            a = np.array(lst, np.int32)              # columns: idx, bid, meta
            self._apply_chunk_edits(ch, a[:, 0], a[:, 1].astype(np.uint8), a[:, 2].astype(np.uint8))
            applied += len(lst)
        return applied

    def repair_world(self, log=None):
        """Comprehensive crash/hazard scan + fix across every loaded chunk. Blocks are
        never touched -- only broken metadata is removed:
          * ORPHANED tile-entities — chest/sign/furnace data on a block that no longer
            matches (the invisible hard-freeze hazard);
          * OUT-OF-RANGE tile-entities — position null or y outside 0..127;
          * DUPLICATE tile-entities — two occupying the exact same block;
          * BROKEN entities — NaN / infinite / absurd position or motion (crash on tick).
        Returns a stats dict; marks touched chunks dirty (call save() to persist)."""
        stats = {"orphan_te": [], "bad_pos_te": 0, "dup_te": 0, "bad_entities": 0, "chunks": 0}
        for name in [n for n in self._filedata if n.endswith(".mcr")]:
            base = name[:-4].split("/")[-1].split(".")
            rx, rz = int(base[1]), int(base[2]); nether = name.startswith("DIM-1")
            reg = self.region(rx, rz, nether)
            reg.decode_all()
            for (lcx, lcz), ch in reg.chunks.items():
                touched = False
                lst = ch.tile_entities
                if lst and lst.items:
                    seen = set(); kept = []
                    for te in lst.items:
                        x, y, z = te.get_value("x"), te.get_value("y"), te.get_value("z")
                        if x is None or y is None or z is None or not (0 <= y < 128):
                            stats["bad_pos_te"] += 1; touched = True; continue
                        if _te_orphaned(te.get_value("id"), ch.get_block(x, y, z)):
                            stats["orphan_te"].append((te.get_value("id"), x, y, z))
                            touched = True; continue
                        if (x, y, z) in seen:
                            stats["dup_te"] += 1; touched = True; continue
                        seen.add((x, y, z)); kept.append(te)
                    if len(kept) != len(lst.items):
                        lst.items[:] = kept
                ents = ch.entities
                if ents and ents.items:
                    kept = [en for en in ents.items if not _entity_broken(en)]
                    if len(kept) != len(ents.items):
                        stats["bad_entities"] += len(ents.items) - len(kept)
                        ents.items[:] = kept; touched = True
                if touched:
                    ch._mark(); stats["chunks"] += 1
        if log:
            log("repair: %d orphaned + %d bad-position + %d duplicate tile-entities, "
                "%d broken entities (%d chunks touched)"
                % (len(stats["orphan_te"]), stats["bad_pos_te"], stats["dup_te"],
                   stats["bad_entities"], stats["chunks"]))
        return stats

    # kept for callers that only want the orphan sweep; repair_world is the full pass.
    def fix_orphaned_tile_entities(self, log=None):
        return self.repair_world(log)["orphan_te"]

    def recompute_lighting(self, log=None):
        """Rebuild HeightMap + SkyLight + BlockLight for every chunk (best-effort per-chunk
        flood-fill). External editors leave these stale; a Beta/TU0 engine trusts them, so
        stale values cause dark builds and misplaced mob spawns. Returns chunk count."""
        import numpy as np
        OP = np.full(256, 15, np.uint8)                       # light opacity (15 = opaque)
        for b in (0, 6, 20, 26, 27, 28, 31, 32, 37, 38, 39, 40, 44, 50, 51, 52, 53, 55,
                  59, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 75, 76, 77, 78, 81, 83, 85,
                  90, 92, 93, 94, 96):
            OP[b] = 0
        OP[10] = OP[11] = 0; OP[8] = OP[9] = 3; OP[79] = 3; OP[18] = 1; OP[30] = 1
        EM = np.zeros(256, np.int16)                          # block-light emission
        EM[50] = 14; EM[10] = EM[11] = 15; EM[51] = 15; EM[89] = 15; EM[91] = 15
        EM[76] = 7; EM[62] = 13; EM[90] = 11; EM[74] = 9

        def prop(light, dec):
            for _ in range(15):
                prev = light.copy()
                light[1:, :, :] = np.maximum(light[1:, :, :], light[:-1, :, :] - dec[1:, :, :])
                light[:-1, :, :] = np.maximum(light[:-1, :, :], light[1:, :, :] - dec[:-1, :, :])
                light[:, 1:, :] = np.maximum(light[:, 1:, :], light[:, :-1, :] - dec[:, 1:, :])
                light[:, :-1, :] = np.maximum(light[:, :-1, :], light[:, 1:, :] - dec[:, :-1, :])
                light[:, :, 1:] = np.maximum(light[:, :, 1:], light[:, :, :-1] - dec[:, :, 1:])
                light[:, :, :-1] = np.maximum(light[:, :, :-1], light[:, :, 1:] - dec[:, :, :-1])
                np.clip(light, 0, 15, out=light)
                if np.array_equal(light, prev):
                    break
            return light

        def pack(a):
            f = a.reshape(-1).astype(np.uint8)
            return list((f[0::2] | (f[1::2] << 4)).astype(np.uint8))

        n = 0
        for name in [nm for nm in self._filedata if nm.endswith(".mcr")]:
            base = name[:-4].split("/")[-1].split(".")
            rx, rz = int(base[1]), int(base[2]); nether = name.startswith("DIM-1")
            reg = self.region(rx, rz, nether)
            reg.decode_all()
            for (lcx, lcz), ch in reg.chunks.items():
                bl = np.frombuffer(bytes(bytearray(ch._blocks())), np.uint8).reshape(16, 16, 128)
                op = OP[bl].astype(np.int16); dec = np.maximum(op, 1)
                yidx = np.arange(128, dtype=np.int16)
                masked = np.where(op > 0, yidx[None, None, :], -1)
                hm = (masked.max(axis=2) + 1).clip(0, 255).astype(np.uint8)
                open_above = np.cumprod((op == 0)[:, :, ::-1].astype(np.int8), axis=2)[:, :, ::-1].astype(bool)
                sky = np.zeros((16, 16, 128), np.int16); sky[open_above] = 15
                sky = prop(sky, dec); sky[op >= 15] = 0
                blk = prop(EM[bl].astype(np.int16), dec)
                ch.level.set("HeightMap", N.BYTE_ARRAY, list(hm.T.reshape(-1)))
                ch.level.set("SkyLight", N.BYTE_ARRAY, pack(sky))
                ch.level.set("BlockLight", N.BYTE_ARRAY, pack(blk))
                ch._mark(); n += 1
        if log:
            log("recomputed lighting for %d chunks" % n)
        return n

    def fill(self, x1, y1, z1, x2, y2, z2, bid, data=0, nether=False):
        """Fill a box with one block, per chunk via numpy slice assignment (fast enough
        to clear/delete large regions in a blink instead of minutes)."""
        import numpy as np
        x1, x2 = sorted((x1, x2)); y1, y2 = sorted((y1, y2)); z1, z2 = sorted((z1, z2))
        y1 = max(0, y1); y2 = min(127, y2)
        if y1 > y2:
            return 0
        bid &= 0xFF; data &= 0xF
        total = 0
        for wcx in range(x1 >> 4, (x2 >> 4) + 1):
            for wcz in range(z1 >> 4, (z2 >> 4) + 1):
                ch = self.chunk(wcx, wcz, nether)
                if ch is None:
                    continue
                lx0, lx1 = max(x1, wcx * 16) & 15, min(x2, wcx * 16 + 15) & 15   # local box
                lz0, lz1 = max(z1, wcz * 16) & 15, min(z2, wcz * 16 + 15) & 15
                # operate on the LOWER 128 section (bytes 0..32767); a 256-tall LCE chunk
                # is [lower-128 | upper-128] and TU0 only uses the lower half
                bl = ch._blocks()
                arr = np.frombuffer(bytes(bytearray(bl[:32768])), np.uint8).reshape(16, 16, 128).copy()
                arr[lx0:lx1 + 1, lz0:lz1 + 1, y1:y2 + 1] = bid
                bl[:32768] = arr.reshape(-1).tolist()
                d = ch._data()
                if d is not None:
                    db = np.frombuffer(bytes(bytearray(d[:16384])), np.uint8)
                    nib = np.empty(32768, np.uint8); nib[0::2] = db & 0x0F; nib[1::2] = db >> 4
                    nib.reshape(16, 16, 128)[lx0:lx1 + 1, lz0:lz1 + 1, y1:y2 + 1] = data
                    d[:16384] = (nib[0::2] | (nib[1::2] << 4)).astype(np.uint8).tolist()
                tes = ch.tile_entities                          # drop tile-entities in the box
                if tes and tes.items:
                    kept = [te for te in tes.items
                            if not (x1 <= (te.get_value("x") or -1) <= x2
                                    and y1 <= (te.get_value("y") or -1) <= y2
                                    and z1 <= (te.get_value("z") or -1) <= z2
                                    and _te_orphaned(te.get_value("id"), bid))]
                    if len(kept) != len(tes.items):
                        tes.items[:] = kept; ch._te_pos_cache = None
                ch._mark()
                total += (lx1 - lx0 + 1) * (lz1 - lz0 + 1) * (y2 - y1 + 1)
        return total

    # -- entities / tiles ---------------------------------------------------
    def add_mob(self, mob_id, x, y, z, nether=False):
        c = self.chunk(x >> 4, z >> 4, nether)
        if c is None:
            raise ValueError("chunk for (%d,%d) not in save" % (x, z))
        c.add_entity(inject.mob_compound(mob_id, x + 0.5, float(y), z + 0.5))

    def add_spawner(self, x, y, z, mob="Giant", delay=20, nether=False):
        c = self.chunk(x >> 4, z >> 4, nether)
        self.set_block(x, y, z, 52, nether=nether)
        c.add_tile_entity(inject.spawner_compound(x, y, z, mob, delay))

    def entities(self, wcx, wcz, nether=False):
        c = self.chunk(wcx, wcz, nether)
        return c.entities if c else None

    # -- save ---------------------------------------------------------------
    def save(self, out=None, backup=True, verify=False, fast=True, progress=None):
        """Write back. out=None saves in place (to the dir's savegame.dat).

        verify defaults OFF: both compressors already self-verify by decoding
        (native `xmem_compress_native` raises on mismatch and falls back; the
        pure-python path verifies each frame), so a second full decode here is
        redundant and just costs ~8s. Pass verify=True to force the extra check.

        progress: optional callback(done, total, text). Reports live chunk-compression
        progress (the slow part of a big save) as a determinate bar, then the final
        payload-compression + write as spinner phases, so a save never looks hung."""
        # flush dirty structures into filedata
        if self._dirty_level:
            self._filedata["level.dat"] = N.serialize(self._level_root_name,
                                                       self._level_root, N.COMPOUND)
        if self._dirty_player:
            self._filedata[self._pkey] = N.serialize(self._player_root_name,
                                                      self.player, N.COMPOUND)
        dirty = [(name, reg) for name, reg in self._regions.items()
                 if reg.dirty or reg.aquatic_edits]
        total = sum(len(reg.dirty) + len(reg.aquatic_edits) for _n, reg in dirty)
        if progress and total:
            import itertools
            counter = itertools.count(1)
            step = max(1, total // 100)                   # cap UI churn to ~100 updates
            progress(0, total, "Compressing chunks… 0/%d" % total)

            def _one():
                d = next(counter)
                if d % step == 0 or d == total:
                    progress(d, total, "Compressing chunks… %d/%d" % (d, total))
        else:
            _one = None
        for name, reg in dirty:
            self._filedata[name] = reg.rebuild(on_chunk=_one)
        if progress:
            progress(0, 0, "Compressing save file…")      # one big payload compress (spinner)
        data = inject.build_retail(self._ents, self._filedata, self._ver, fast=fast)

        # resolve output path
        if out is None:
            out = self.path
        if os.path.isdir(out):
            out = os.path.join(out, "savegame.dat")
        if backup and os.path.exists(out):
            bak = out + ".bak"
            if not os.path.exists(bak):
                shutil.copy(out, bak)
        if verify:
            # round-trip: the rebuilt container must decode back to the same files
            blob, _ = codec_recover_decode(data)
            io_, cnt, files = _reparse_vfs(blob)
            names = {f[0] for f in files}
            missing = set(self._filedata) - names
            if missing:
                raise RuntimeError("verify failed: files missing after rebuild: %s" % missing)
        if progress:
            progress(0, 0, "Writing save…")
        open(out, "wb").write(data)
        return out
