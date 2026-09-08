"""World analytics — a rich census of a save: block/ore counts, chest loot tables,
mob + spawner census, and the rarest/most-valuable items you own. Renders a
self-contained HTML report.

    from lce import analytics
    stats = analytics.analyze(save_path)
    open("report.html", "w", encoding="utf-8").write(analytics.report_html(stats))
"""
import collections
import html

import numpy as np

from ..view3d.world import World
from ..view3d import blocks as B

_ORES = {16: "Coal", 15: "Iron", 14: "Gold", 56: "Diamond", 21: "Lapis",
         73: "Redstone", 74: "Redstone", 129: "Emerald", 153: "Quartz"}

# numeric item ids (Beta/LCE) that aren't blocks (>=256); blocks (<256) use B.name.
_ITEMS = {
    256: "iron shovel", 257: "iron pickaxe", 258: "iron axe", 259: "flint & steel",
    260: "apple", 261: "bow", 262: "arrow", 263: "coal", 264: "diamond", 265: "iron ingot",
    266: "gold ingot", 267: "iron sword", 268: "wooden sword", 269: "wooden shovel",
    270: "wooden pickaxe", 271: "wooden axe", 272: "stone sword", 273: "stone shovel",
    274: "stone pickaxe", 275: "stone axe", 276: "diamond sword", 277: "diamond shovel",
    278: "diamond pickaxe", 279: "diamond axe", 280: "stick", 281: "bowl", 282: "mushroom stew",
    283: "golden sword", 284: "golden shovel", 285: "golden pickaxe", 286: "golden axe",
    287: "string", 288: "feather", 289: "gunpowder", 290: "wooden hoe", 291: "stone hoe",
    292: "iron hoe", 293: "diamond hoe", 294: "golden hoe", 295: "seeds", 296: "wheat",
    297: "bread", 298: "leather cap", 299: "leather tunic", 300: "leather pants",
    301: "leather boots", 302: "chainmail helmet", 303: "chainmail chestplate",
    304: "chainmail leggings", 305: "chainmail boots", 306: "iron helmet",
    307: "iron chestplate", 308: "iron leggings", 309: "iron boots", 310: "diamond helmet",
    311: "diamond chestplate", 312: "diamond leggings", 313: "diamond boots",
    314: "golden helmet", 315: "golden chestplate", 316: "golden leggings", 317: "golden boots",
    318: "flint", 319: "raw porkchop", 320: "cooked porkchop", 321: "painting",
    322: "golden apple", 323: "sign", 324: "wooden door", 325: "bucket", 326: "water bucket",
    327: "lava bucket", 328: "minecart", 329: "saddle", 330: "iron door", 331: "redstone",
    332: "snowball", 333: "boat", 334: "leather", 335: "milk bucket", 336: "brick",
    337: "clay", 338: "sugar cane", 339: "paper", 340: "book", 341: "slimeball",
    342: "storage minecart", 343: "powered minecart", 344: "egg", 345: "compass",
    346: "fishing rod", 347: "clock", 348: "glowstone dust", 349: "raw fish",
    350: "cooked fish", 351: "dye", 352: "bone", 353: "sugar", 354: "cake", 355: "bed",
    356: "redstone repeater", 357: "cookie", 358: "map", 359: "shears", 360: "melon",
    361: "pumpkin seeds", 362: "melon seeds", 363: "raw beef", 364: "steak",
    365: "raw chicken", 366: "cooked chicken", 367: "rotten flesh", 368: "ender pearl",
    369: "blaze rod", 370: "ghast tear", 371: "gold nugget", 372: "nether wart",
    373: "potion", 374: "glass bottle", 375: "spider eye", 376: "fermented spider eye",
    377: "blaze powder", 378: "magma cream", 379: "brewing stand", 380: "cauldron",
    381: "eye of ender", 382: "glistering melon", 383: "spawn egg", 384: "xp bottle",
    385: "fire charge", 386: "book and quill", 387: "written book", 388: "emerald",
    389: "item frame", 390: "flower pot", 400: "pumpkin pie",
    2256: "music disc", 2257: "music disc", 2258: "music disc",
}
_VALUABLE = {"diamond", "emerald", "gold ingot", "golden apple", "diamond sword",
             "diamond pickaxe", "diamond chestplate", "diamond helmet", "diamond leggings",
             "diamond boots", "enchanted book", "nether star", "netherite ingot"}


def _item_name(iid):
    if isinstance(iid, str):
        return iid.split(":")[-1].replace("_", " ")
    if iid < 256:
        return B.name(iid) or ("block %d" % iid)
    return _ITEMS.get(iid, "item %d" % iid)


def analyze(save_path):
    """Census a save -> a stats dict for report_html."""
    w = World.load(save_path)
    # block histogram + ore census (vectorised over all chunks)
    hist = np.zeros(256, np.int64)
    for arr in w.chunks.values():
        hist += np.bincount(arr.reshape(-1), minlength=256)
    blocks = [(B.name(i) or "block %d" % i, int(hist[i])) for i in range(1, 256) if hist[i]]
    blocks.sort(key=lambda kv: -kv[1])
    ores = collections.Counter()
    for bid, nm in _ORES.items():
        ores[nm] += int(hist[bid])
    # mobs + spawners
    mobs = collections.Counter()
    spawners = []
    for kind, nm, x, y, z in w.entities:
        base = nm.split(":")[-1].lower()
        if kind == "mob":
            mobs[nm.split(":")[-1]] += 1
        elif "spawner" in base:
            spawners.append((nm, int(x), int(y), int(z)))
    # chest loot
    loot = collections.Counter()
    for _nm, _x, _y, _z, items in w.containers:
        for iid, cnt in items:
            loot[_item_name(iid)] += cnt
    valuables = [(n, c) for n, c in loot.items() if n in _VALUABLE]
    valuables.sort(key=lambda kv: -kv[1])
    rarest = sorted(((n, c) for n, c in loot.items()), key=lambda kv: kv[1])[:12]
    return {
        "name": w.name, "spawn": w.spawn, "chunks": len(w.chunks),
        "nonair": int(hist[1:].sum()),
        "blocks": blocks[:20], "ores": dict(ores),
        "mobs": mobs.most_common(20), "n_mobs": sum(mobs.values()),
        "spawners": spawners, "containers": len(w.containers),
        "loot": loot.most_common(25), "n_items": sum(loot.values()),
        "valuables": valuables, "rarest": rarest,
    }


def _rows(pairs, cls=""):
    return "\n".join("<tr><td>%s</td><td class='n'>%s</td></tr>"
                     % (html.escape(str(k)), "{:,}".format(v)) for k, v in pairs)


def report_html(s):
    e = html.escape
    cards = [("Chunks", "{:,}".format(s["chunks"])), ("Non-air blocks", "{:,}".format(s["nonair"])),
             ("Mobs", "{:,}".format(s["n_mobs"])), ("Chests & containers", "{:,}".format(s["containers"])),
             ("Items stored", "{:,}".format(s["n_items"])), ("Spawners", "{:,}".format(len(s["spawners"])))]
    card_html = "".join("<div class='card'><div class='cv'>%s</div><div class='cl'>%s</div></div>"
                        % (v, l) for l, v in cards)
    ore_rows = _rows([(k, v) for k, v in s["ores"].items() if v])
    spawn_rows = "\n".join("<tr><td>%s</td><td class='n'>%d, %d, %d</td></tr>"
                           % (e(n.split(':')[-1]), x, y, z) for n, x, y, z in s["spawners"][:30])
    val_rows = _rows(s["valuables"]) or "<tr><td colspan=2 class='muted'>no notable valuables found</td></tr>"
    return """<!doctype html><meta charset=utf-8><title>%s — world report</title><style>
:root{color-scheme:dark}body{margin:0;background:#0f1116;color:#e6e9ef;font:14px/1.5 system-ui,Segoe UI,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px}
h1{font-size:26px;margin:0 0 2px}.sub{color:#8b93a7;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px}
.card{background:#181b23;border:1px solid #262b37;border-radius:12px;padding:14px 16px}
.cv{font-size:24px;font-weight:700}.cl{color:#8b93a7;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:#aab2c5;border-bottom:1px solid #262b37;padding-bottom:6px;margin:24px 0 8px}
table{width:100%%;border-collapse:collapse}td{padding:4px 2px;border-bottom:1px solid #1c2029}
td.n{text-align:right;color:#9fd4a3;font-variant-numeric:tabular-nums}.muted{color:#6b7385}
.bar{height:7px;background:#2b6cb0;border-radius:4px;margin-top:3px}
</style><div class=wrap>
<h1>%s</h1><div class=sub>spawn %d, %d, %d · %s chunks explored</div>
<div class=cards>%s</div>
<div class=grid>
<div><h2>Ore census</h2><table>%s</table>
<h2>Most valuable loot</h2><table>%s</table></div>
<div><h2>Top blocks</h2><table>%s</table></div>
</div>
<div class=grid>
<div><h2>Chest loot — most stored</h2><table>%s</table></div>
<div><h2>Mobs</h2><table>%s</table>
<h2>Rarest items</h2><table>%s</table></div>
</div>
<div><h2>Spawners (%d)</h2><table>%s</table></div>
</div>""" % (
        e(s["name"]), e(s["name"]), s["spawn"][0], s["spawn"][1], s["spawn"][2],
        "{:,}".format(s["chunks"]), card_html,
        ore_rows, val_rows, _rows(s["blocks"]),
        _rows(s["loot"]), _rows(s["mobs"]), _rows(s["rarest"]),
        len(s["spawners"]), spawn_rows or "<tr><td class=muted colspan=2>none</td></tr>")
