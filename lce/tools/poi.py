"""Structure & points-of-interest finder for an LCE world.

Scans every chunk once and reports the interesting places -- dungeons (mob spawners),
loot chests (flagging valuable ones), nether portals, stronghold end-portals, labelled
signs, and player builds (clusters of crafted blocks) -- each with world coords so the
GUI/viewer can jump to and mark them.
"""
import numpy as np

# blocks that signal a PLAYER build (crafted/refined). Natural-but-ambiguous ids
# (cobblestone 4, sandstone 24, dirt, stone) are deliberately excluded so caves and
# deserts don't register as builds.
_CRAFTED = {5, 20, 22, 25, 35, 41, 42, 43, 44, 45, 46, 47, 53, 57, 58, 61, 64, 65, 67,
            84, 85, 89, 91, 98, 101, 102, 107, 108, 109, 155, 159}
_BUILD_MIN = 45                      # crafted blocks in a chunk to count as a build

# item ids that make a chest "valuable loot"
_VALUABLE = {264: "diamond", 266: "gold ingot", 265: "iron ingot", 322: "golden apple",
             329: "saddle", 57: "diamond block", 41: "gold block", 42: "iron block",
             276: "diamond sword", 277: "diamond shovel", 278: "diamond pickaxe",
             279: "diamond axe", 310: "diamond helmet", 311: "diamond chestplate",
             312: "diamond leggings", 313: "diamond boots", 331: "redstone", 348: "glowstone",
             2256: "music disc", 2257: "music disc"}

_MOB_LABEL = {"Zombie": "zombie", "Skeleton": "skeleton", "Spider": "spider",
              "Creeper": "creeper", "CaveSpider": "cave spider", "Silverfish": "silverfish",
              "Blaze": "blaze", "PigZombie": "zombie pigman"}


def _valuable_items(items):
    hits = []
    if items:
        for it in items.items:
            t = it.get_tag("id")
            if t is None:
                continue
            v = int(t.value) if not isinstance(t.value, str) else -1
            if v in _VALUABLE:
                hits.append(_VALUABLE[v])
            elif it.get_tag("tag") is not None or it.get_tag("ench") is not None:
                hits.append("enchanted")
    return hits


def _cluster(points, radius):
    """Greedy spatial clustering: merge points within `radius` (Chebyshev). points =
    list of (x,y,z, payload). Returns list of clusters: (cx,cy,cz, [payloads])."""
    clusters = []
    for x, y, z, pl in points:
        for c in clusters:
            if abs(x - c[0]) <= radius and abs(z - c[2]) <= radius and abs(y - c[1]) <= radius + 6:
                c[3].append(pl)
                n = len(c[3])
                c[0] = (c[0] * (n - 1) + x) // n; c[1] = (c[1] * (n - 1) + y) // n
                c[2] = (c[2] * (n - 1) + z) // n
                break
        else:
            clusters.append([x, y, z, [pl]])
    return clusters


def find_pois(world, log=None):
    """Scan the whole world; return a list of POI dicts sorted most-significant first:
    {type, x, y, z, label, count, dim}."""
    spawners, chests, signs, portals, endframes = [], [], [], [], []
    build_chunks = {}                                    # (gx,gz,dim) -> (count, top-y, dominant id)

    from ..world import Region
    for name in [n for n in world._filedata if n.endswith(".mcr")]:
        base = name[:-4].split("/")[-1].split(".")
        rx, rz = int(base[1]), int(base[2])
        # keys: overworld "r.X.Z.mcr", nether "DIM-1r.X.Z.mcr", END "DIM1/r.X.Z.mcr"
        dim = -1 if name.startswith("DIM-1") else (1 if name.startswith("DIM1/") else 0)
        try:
            reg = Region(world, name)        # by exact key -> the End is addressed correctly
            reg.decode_all()
        except Exception:
            continue
        for (lcx, lcz), ch in reg.chunks.items():
            gx, gz = rx * 32 * 16 + lcx * 16, rz * 32 * 16 + lcz * 16
            te = ch.tile_entities
            if te and te.items:
                for t in te.items:
                    tid = t.get_value("id")
                    x, y, z = t.get_value("x"), t.get_value("y"), t.get_value("z")
                    if x is None:
                        continue
                    if tid == "MobSpawner":
                        spawners.append((x, y, z, t.get_value("EntityId") or "?"))
                    elif tid == "Chest":
                        chests.append((x, y, z, _valuable_items(t.get_value("Items"))))
                    elif tid == "Sign":
                        txt = " ".join(str(t.get_value("Text%d" % k) or "") for k in (1, 2, 3, 4)).strip()
                        if txt:
                            signs.append((x, y, z, txt))
            bl = np.frombuffer(bytes(bytearray(ch._blocks()[:32768])), np.uint8)
            if (bl == 90).any():                         # nether portal
                ys = np.where(bl == 90)[0] % 128
                portals.append((gx + 8, int(ys.mean()), gz + 8, int((bl == 90).sum())))
            if (bl == 120).any():                        # end portal frame -> stronghold
                endframes.append((gx + 8, 64, gz + 8, int((bl == 120).sum())))
            crafted = np.isin(bl, list(_CRAFTED))
            c = int(crafted.sum())
            if c >= _BUILD_MIN:
                dom = int(np.bincount(bl[crafted]).argmax())
                build_chunks[(rx * 32 + lcx, rz * 32 + lcz, dim)] = (c, gx, gz, dom)

    pois = []
    # dungeons (spawner clusters)
    for cx, cy, cz, mobs in _cluster([(x, y, z, m) for x, y, z, m in spawners], 8):
        kinds = sorted(set(_MOB_LABEL.get(m, m) for m in mobs))
        pois.append(dict(type="dungeon", x=cx, y=cy, z=cz, dim=0, count=len(mobs),
                         label="%s dungeon" % "/".join(kinds), score=90 + len(mobs)))
    # loot (chest clusters)
    for cx, cy, cz, loots in _cluster([(x, y, z, l) for x, y, z, l in chests], 6):
        val = sorted(set(v for l in loots for v in l))
        lbl = "%d chest%s" % (len(loots), "s" if len(loots) != 1 else "")
        score = 40 + len(loots)
        if val:
            lbl += " ★ " + ", ".join(val[:4]); score += 40
        pois.append(dict(type="loot", x=cx, y=cy, z=cz, dim=0, count=len(loots),
                         label=lbl, score=score))
    # portals / strongholds
    for x, y, z, n in portals:
        pois.append(dict(type="portal", x=x, y=y, z=z, dim=0, count=n,
                         label="nether portal", score=70))
    for c in _cluster([(x, y, z, 1) for x, y, z, _n in endframes], 12):
        pois.append(dict(type="stronghold", x=c[0], y=c[1], z=c[2], dim=0, count=len(c[3]),
                         label="stronghold (end portal)", score=100))
    # labelled signs
    for x, y, z, txt in signs:
        pois.append(dict(type="sign", x=x, y=y, z=z, dim=0, count=1,
                         label="sign: " + (txt[:40]), score=30))
    # builds (merge adjacent build-chunks)
    seen = set()
    keys = set(build_chunks)
    for key in list(keys):
        if key in seen:
            continue
        stack = [key]; group = []
        while stack:
            k = stack.pop()
            if k in seen or k not in build_chunks:
                continue
            seen.add(k); group.append(k)
            cxk, czk, dk = k
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                stack.append((cxk + dx, czk + dz, dk))
        tot = sum(build_chunks[k][0] for k in group)
        cx = sum(build_chunks[k][1] for k in group) // len(group) + 8
        cz = sum(build_chunks[k][2] for k in group) // len(group) + 8
        dom = max(group, key=lambda k: build_chunks[k][0])
        domid = build_chunks[dom][3]
        pois.append(dict(type="build", x=cx, y=72, z=cz, dim=group[0][2], count=len(group),
                         label="build (%d chunks, mostly %s)" % (len(group), _blockname(domid)),
                         score=20 + min(len(group), 40)))

    pois.sort(key=lambda p: -p["score"])
    if log:
        from collections import Counter
        by = Counter(p["type"] for p in pois)
        log("found %d POIs: %s" % (len(pois), dict(by)))
    return pois


def _blockname(bid):
    names = {5: "planks", 35: "wool", 20: "glass", 53: "wood stairs", 45: "brick",
             98: "stone brick", 89: "glowstone", 41: "gold", 42: "iron", 57: "diamond",
             85: "fence", 47: "bookshelf", 24: "sandstone", 44: "slab", 67: "cobble stairs"}
    return names.get(bid, "id %d" % bid)
