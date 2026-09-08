"""Global NBT search — find any tag, id, item, entity or name across a whole save.

Searches the top-level NBT files (level.dat, players/*, data/*) and every chunk's
Entities + TileEntities across all dimensions. A query is whitespace-split into
tokens; a unit matches when EVERY token is a substring of its flattened text
(tag names + string/number values + numeric item/block ids resolved to names).

    from lce import nbtsearch as S
    hits = S.search(world, "spawner")            # or "command", "diamond sword", "minecraft:chest"
    # each hit: {kind, id, where, pos, tag (Compound), summary}
"""
import re

from .. import nbt as N
from .. import names as NM

_NUM = (N.BYTE, N.SHORT, N.INT, N.LONG, N.FLOAT, N.DOUBLE)
_RGX = re.compile(r"^(DIM-1|DIM1/)?r\.(-?\d+)\.(-?\d+)\.mcr$")   # End keys are "DIM1/r.X.Z.mcr"
_DIM = {"": "Overworld", "DIM-1": "Nether", "DIM1": "End"}


def _walk(comp, out):
    for name, tag in comp.items():
        out.append(name.lower())
        _walk_val(tag.id, tag.value, name, out)


def _walk_val(tid, val, name, out):
    if tid == N.STRING:
        out.append(str(val).lower())
    elif tid in _NUM:
        out.append(str(val).lower())
        if name == "id" and isinstance(val, int):          # numeric item/block id -> its name
            nm = NM.ITEM_NAMES.get(val) or NM.BLOCK_NAMES.get(val)
            if nm:
                out.append(nm.lower())
    elif tid == N.COMPOUND:
        _walk(val, out)
    elif tid == N.LIST:
        for it in val.items:
            if val.etype == N.COMPOUND:
                _walk(it, out)
            elif val.etype == N.STRING:
                out.append(str(it).lower())
            elif val.etype in _NUM:
                out.append(str(it).lower())
    # byte/int arrays (block planes etc.) are intentionally skipped


def _matches(comp, toks):
    out = []
    _walk(comp, out)
    hay = " ".join(out)
    return all(t in hay for t in toks)


def _display_name(comp):
    """A human label from CustomName / display.Name / sign Text, if any."""
    cn = comp.get_value("CustomName")
    if cn:
        return str(cn)
    disp = comp.get_tag("tag")
    if disp is not None and isinstance(disp.value, N.Compound):
        d = disp.value.get_tag("display")
        if d is not None and isinstance(d.value, N.Compound):
            nm = d.value.get_value("Name")
            if nm:
                return str(nm)
    texts = [comp.get_value("Text%d" % i) for i in range(1, 5)]
    texts = [t for t in texts if t]
    if texts:
        return " ".join(texts)[:40]
    return None


def _te_hit(te, dim, cx, cz):
    tid = te.get_value("id") or "?"
    x, y, z = te.get_value("x"), te.get_value("y"), te.get_value("z")
    dn = _display_name(te)
    return {"kind": "tile-entity", "id": tid, "where": "%s  chunk %d,%d" % (dim, cx, cz),
            "pos": (x, y, z), "tag": te,
            "summary": tid + (" — “%s”" % dn if dn else "")}


def _en_hit(en, dim, cx, cz):
    tid = en.get_value("id") or "?"
    pos = en.get_value("Pos")
    xyz = None
    if pos is not None and hasattr(pos, "items") and len(pos.items) == 3:
        xyz = tuple(int(round(v)) for v in pos.items)
    dn = _display_name(en)
    return {"kind": "entity", "id": tid, "where": "%s  chunk %d,%d" % (dim, cx, cz),
            "pos": xyz, "tag": en,
            "summary": tid + (" — “%s”" % dn if dn else "")}


def _iter_chunks(world, log=None):
    from ..world import Region
    for name in list(world._filedata):
        m = _RGX.match(name)
        if not m:
            continue
        dim = _DIM.get((m.group(1) or "").rstrip("/"), "Overworld")
        rx, rz = int(m.group(2)), int(m.group(3))
        try:
            reg = Region(world, name)
            reg.decode_all()
        except Exception:
            continue
        for lcx in range(32):
            for lcz in range(32):
                ch = reg.chunk(lcx, lcz)
                if ch is not None:
                    yield dim, rx * 32 + lcx, rz * 32 + lcz, ch
        if log:
            log("searching %s…" % name)


def _hay(comp):
    out = []
    _walk(comp, out)
    return " ".join(out)


def build_index(world, log=None):
    """Decode the whole save ONCE into searchable records, each carrying a precomputed
    lowercased haystack under 'hay' plus its hit metadata (kind/id/where/pos/tag/summary).
    Slow (decompresses every region) — do it on a worker thread and cache the result;
    then filter_index() is instant for every subsequent query."""
    index = []
    for fname in list(world._filedata):
        if not fname.endswith(".dat"):
            continue
        try:
            _n, tag, _ = N.parse_tag(world._filedata[fname])
            comp = tag.value
        except Exception:
            continue
        if isinstance(comp, N.Compound):
            rec = {"kind": "file", "id": fname, "where": fname, "pos": None,
                   "tag": comp, "summary": fname, "hay": _hay(comp)}
            index.append(rec)
    for dim, cx, cz, ch in _iter_chunks(world, log):
        tes = ch.tile_entities
        for te in (tes.items if tes else []):
            rec = _te_hit(te, dim, cx, cz); rec["hay"] = _hay(te); index.append(rec)
        ents = ch.entities
        for en in (ents.items if ents else []):
            rec = _en_hit(en, dim, cx, cz); rec["hay"] = _hay(en); index.append(rec)
    return index


def filter_index(index, query, limit=3000):
    """Instantly filter a built index by `query` (whitespace tokens, all must match)."""
    toks = [t for t in str(query).strip().lower().split() if t]
    if not toks:
        return []
    out = []
    for rec in index:
        hay = rec["hay"]
        if all(t in hay for t in toks):
            out.append(rec)
            if len(out) >= limit:
                break
    return out


def search(world, query, log=None):
    """Convenience: build the index and filter in one call."""
    return filter_index(build_index(world, log), query)
