"""Unified command line for LCE Studio.

    python -m lce info   <save>
    python -m lce inv    <save> [--add ID[:COUNT[:DMG]]] ... [--clear]
    python -m lce block  <save> get X Y Z | set X Y Z ID[:DATA] | fill X1 Y1 Z1 X2 Y2 Z2 ID
    python -m lce mob    <save> add NAME X Y Z | list WCX WCZ
    python -m lce spawner <save> add X Y Z [MOB] [DELAY]
    python -m lce level  <save> [--spawn X Y Z] [--set KEY VALUE] ...
    python -m lce nbt    <save> <vfs-file>            # dump an NBT tree
    python -m lce ls     <save>                       # list VFS files
    python -m lce analyze <container.zip|dir>         # health / recovery report
    python -m lce convert <java-world> <template.bin> <out.bin> [name]  # Java -> TU0
    python -m lce stfs   <pkg.bin> [extract NAME OUT] # inspect/extract CON/LIVE/PIRS
    python -m lce reassign <con.bin> <new-xuid> [out] # move a save to a new account
    python -m lce unpack <con> <out-folder>           # CON package -> Nexia save folder
    python -m lce unpackall <dir>                      # unpack every CON in a saves dir
    python -m lce pack   <folder> <out.con> <template-con> [name]  # save folder -> CON
    python -m lce map    <save> <out.png> [scale]      # top-down world atlas
    python -m lce mapitems <save> <out-dir>            # render in-game map_*.dat items
    python -m lce schem export <save> X0 Y0 Z0 X1 Y1 Z1 <out.schematic>
    python -m lce schem import <save> <in.schematic> X Y Z   # stamp + write (.bak)
    python -m lce flatten <save> [--nokeep]            # cut terrain to sea level
    python -m lce mountains <save> [MAXH] [COVERAGE] [SEED]  # add noise mountains
    python -m lce report <save> <out.html>             # world analytics report
"""
import sys

from . import recover
from . import nbt as N
from .world import World


def _open(path):
    return World.open(path)


def cmd_info(a):
    w = _open(a[0])
    sx, sy, sz = w.get_spawn()
    print("save   :", a[0])
    print("spawn  : (%s, %s, %s)" % (sx, sy, sz))
    print("player :", w.get_player_pos(), " health=%s" % w.player.get_value("Health"))
    print("items  : %d in inventory" % len(w.inventory()))
    regs = [n for n in w._filedata if n.endswith(".mcr")]
    print("regions: %d (%s)" % (len(regs), ", ".join(sorted(regs))))
    print("files  : %d VFS entries, format=retail v%d" % (len(w._filedata), w._ver))


def cmd_ls(a):
    w = _open(a[0])
    for name, _of, ln, _ts in w._ents:
        print("  %-32s %8d bytes" % (name, ln))


def cmd_inv(a):
    w = _open(a[0]); rest = a[1:]
    changed = False
    i = 0
    while i < len(rest):
        if rest[i] == "--clear":
            w.inventory_clear(); changed = True; i += 1
        elif rest[i] == "--add":
            spec = rest[i + 1].split(":")
            iid = int(spec[0]); cnt = int(spec[1]) if len(spec) > 1 else 1
            dmg = int(spec[2]) if len(spec) > 2 else 0
            slot = w.inventory_add(iid, cnt, dmg)
            print("  + item %d x%d -> slot %d" % (iid, cnt, slot)); changed = True; i += 2
        else:
            i += 1
    if changed:
        print("saved:", w.save(backup=True))
    else:
        for it in w.inventory():
            print("  slot %-2s  id=%-4s x%-3s dmg=%s" % (it["Slot"], it["id"], it["Count"], it["Damage"]))


def cmd_block(a):
    w = _open(a[0]); op = a[1]
    if op == "get":
        x, y, z = map(int, a[2:5])
        print("  block(%d,%d,%d) = %d" % (x, y, z, w.get_block(x, y, z)))
        return
    if op == "set":
        x, y, z = map(int, a[2:5]); spec = a[5].split(":")
        bid = int(spec[0]); dat = int(spec[1]) if len(spec) > 1 else 0
        w.set_block(x, y, z, bid, dat); print("saved:", w.save(backup=True))
        return
    if op == "fill":
        c = list(map(int, a[2:8])); bid = int(a[8])
        n = w.fill(*c, bid); print("  filled %d blocks; saved: %s" % (n, w.save(backup=True)))
        return
    print("block: get|set|fill")


def cmd_mob(a):
    w = _open(a[0]); op = a[1]
    if op == "add":
        name = a[2]; x, y, z = map(int, a[3:6])
        w.add_mob(name, x, y, z); print("  + %s at (%d,%d,%d); saved: %s" % (name, x, y, z, w.save(backup=True)))
        return
    if op == "list":
        wcx, wcz = int(a[2]), int(a[3]); e = w.entities(wcx, wcz)
        if not e:
            print("  (no entities in chunk %d,%d)" % (wcx, wcz)); return
        for it in e.items:
            pos = it.get_value("Pos")
            print("  %-12s @ %s" % (it.get_value("id"), [round(p, 1) for p in pos.items] if pos else "?"))
        return
    print("mob: add|list")


def cmd_spawner(a):
    w = _open(a[0]); op = a[1]
    if op == "add":
        x, y, z = map(int, a[2:5])
        mob = a[5] if len(a) > 5 else "Giant"; delay = int(a[6]) if len(a) > 6 else 20
        w.add_spawner(x, y, z, mob, delay)
        print("  + %s spawner at (%d,%d,%d); saved: %s" % (mob, x, y, z, w.save(backup=True)))


def cmd_level(a):
    w = _open(a[0]); rest = a[1:]
    if not rest:
        for name, tag in w.level.items():
            print("  %-16s %-8s %s" % (name, N.NAMES.get(tag.id), tag.value if tag.id < 7 else "..."))
        return
    i = 0
    while i < len(rest):
        if rest[i] == "--spawn":
            w.set_spawn(int(rest[i + 1]), int(rest[i + 2]), int(rest[i + 3])); i += 4
        elif rest[i] == "--set":
            k, v = rest[i + 1], rest[i + 2]
            try:
                v = int(v)
            except ValueError:
                pass
            w.set_level(k, v); i += 3
        else:
            i += 1
    print("saved:", w.save(backup=True))


def cmd_nbt(a):
    w = _open(a[0]); vf = a[1]
    data = w._filedata.get(vf) or w._filedata.get(vf + ".dat")
    if data is None:
        print("no VFS file %r (try one of: %s)" % (vf, ", ".join(w._filedata))); return
    name, tag, _ = N.parse_tag(data)
    print(N.dumps(name or vf, tag))


def cmd_analyze(a):
    recover.print_report(a[0])


def cmd_convert(a):
    # convert <java_world> <template.bin> <out.bin> [level_name]
    from . import convert
    name = a[3] if len(a) > 3 else None
    convert.convert(a[0], a[1], a[2], level_name=name)


def cmd_tojava(a):
    # tojava <lce_save.bin> <out_dir> [level_name]
    from . import convert
    convert.to_java(a[0], a[1], level_name=a[2] if len(a) > 2 else None)


def cmd_stfs(a):
    # stfs <package.bin> [extract NAME OUT]
    import struct
    from . import stfs
    data = open(a[0], "rb").read()
    s = stfs.STFS(data)
    print("magic     :", data[:4].decode("latin1"))
    print("type      :", struct.unpack_from(">I", data, 0x344)[0], "(1=SavedGame)")
    print("titleID   : %08X" % struct.unpack_from(">I", data, 0x360)[0])
    print("display   :", s.display_name)
    print("profileID :", data[0x371:0x379].hex())
    print("consoleID :", data[0x36C:0x371].hex(), " deviceID:", data[0x3FD:0x3FD + 0x14].hex())
    for nm, (_st, _bl, sz) in s.files.items():
        print("  file %-24s %d bytes" % (nm, sz))
    if len(a) >= 4 and a[1] == "extract":
        open(a[3], "wb").write(s.read_file(a[2]))
        print("extracted", a[2], "->", a[3])


def cmd_reassign(a):
    # reassign <con.bin> <new_xuid> [out.bin]
    from . import stfs
    out = a[2] if len(a) > 2 else a[0].rsplit(".", 1)[0] + ".reassigned.bin"
    data = open(a[0], "rb").read()
    s = stfs.STFS(data)
    print("package    :", s.display_name)
    print("old profile:", data[0x371:0x379].hex())
    newdata = stfs.reassign_profile(data, a[1])
    open(out, "wb").write(newdata)
    print("new profile:", newdata[0x371:0x379].hex(), "(console/device zeroed)")
    print("wrote       :", out, "-- loads on Nexia360 / RGH-JTAG (retail needs KV resign)")


def cmd_unpack(a):
    # unpack <con> <out-folder>
    from . import stfs
    info = stfs.unpack_con(a[0], a[1])
    print("unpacked %r -> %s" % (info["name"], a[1]))
    print("  files:", ", ".join(info["files"]))


def cmd_unpackall(a):
    # unpackall <dir> -- unpack every CON with a savegame.dat into a same-named folder
    import os
    import shutil
    from . import stfs
    d = a[0]
    done = skipped = failed = 0
    for e in sorted(os.listdir(d)):
        p = os.path.join(d, e)
        if not (os.path.isfile(p) and stfs.is_con(p)):
            continue
        try:
            s = stfs.STFS(open(p, "rb").read())
            if not any(n.endswith("savegame.dat") for n in s.files):
                print("  skip  %-34s (%s: no savegame.dat)" % (e, s.display_name)); skipped += 1; continue
            tmp = p + ".__unpacking"
            if os.path.exists(tmp):
                shutil.rmtree(tmp)
            stfs.unpack_con(p, tmp)
            World.open(tmp)                                   # verify before replacing
            final = e if e.lower().endswith(".bin") else e + ".bin"
            os.remove(p); os.rename(tmp, os.path.join(d, final))
            print("  ok    %-34s (%s)" % (final, s.display_name)); done += 1
        except Exception as ex:
            try: shutil.rmtree(tmp)
            except Exception: pass
            print("  FAIL  %-34s %s" % (e, ex)); failed += 1
    print("unpacked %d, skipped %d, failed %d" % (done, skipped, failed))


def cmd_pack(a):
    # pack <folder> <out.con> <template-con> [name]
    from . import stfs
    name = a[3] if len(a) > 3 else None
    out = stfs.build_con(a[0], a[1], template=a[2], display_name=name)
    s = stfs.STFS(open(out, "rb").read())
    print("packed %s -> %s" % (a[0], out))
    print("  name=%r  files=%s  loads on Nexia360 / RGH-JTAG (retail needs KV resign)"
          % (s.display_name, list(s.files)))


def cmd_map(a):
    # map <save> <out.png> [scale]
    from . import atlas
    img = atlas.render_world(atlas.load(a[0]))
    scale = int(a[2]) if len(a) > 2 else 1
    if scale > 1:
        from PIL import Image
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    img.save(a[1])
    print("wrote %dx%d world map -> %s" % (img.width, img.height, a[1]))


def cmd_mapitems(a):
    # mapitems <save> <out-dir>
    import os
    from . import atlas
    lw = World.open(a[0])
    os.makedirs(a[1], exist_ok=True)
    n = 0
    for k in [x for x in lw._filedata if x.startswith("data/map_")]:
        img = atlas.render_map_item(lw._filedata[k])
        if img is not None:
            img.save(os.path.join(a[1], k.split("/")[-1] + ".png")); n += 1
    print("rendered %d map item(s) -> %s" % (n, a[1]))


def cmd_schem(a):
    # schem export <save> x0 y0 z0 x1 y1 z1 <out>  |  schem import <save> <in> x y z
    from . import schematic as S
    from .view3d.world import World as VW
    if a[0] == "export":
        save = a[1]; nums = list(map(int, a[2:8])); out = a[8]
        w = VW.load(save)
        b, d = S.extract(w, *nums)
        tes = []                                            # carry chests/signs from the save
        try:
            tes = S.extract_tile_entities(World.open(save), *nums)
        except Exception as e:
            print("  (tile-entities skipped: %s)" % e)
        W, H, L = S.save(b, d, out, name=out.rsplit(".", 1)[0].split("/")[-1].split("\\")[-1],
                         tile_entities=tes)
        print("exported %dx%dx%d schematic (+%d chest/sign) -> %s" % (W, H, L, len(tes), out))
    elif a[0] == "import":
        save = a[1]; schem = a[2]; x, y, z = map(int, a[3:6])
        w = VW.load(save)
        b, d, tes = S.load(schem)
        n, _dirty = S.stamp(w, b, d, x, y, z)
        if tes:                                             # stamped into the save on export()
            w.pasted_tes.append((tes, x, y, z))
        path, applied = w.export(backup=True)
        print("stamped %d blocks (+%d chest/sign) at (%d,%d,%d); wrote %d edits (.bak kept) -> %s"
              % (n, len(tes), x, y, z, applied, path))
    else:
        print("usage: schem export|import ...")


def cmd_flatten(a):
    # flatten <save> [--nokeep]
    from . import worldgen
    w = World.open(a[0])
    nc, cl = worldgen.flatten(w, keep_builds="--nokeep" not in a)
    path = w.save(backup=True)
    print("flattened %d chunks, cleared %d blocks -> %s (.bak kept)" % (nc, cl, path))


def cmd_mountains(a):
    # mountains <save> [maxh] [coverage] [seed]
    from . import worldgen
    mh = int(a[1]) if len(a) > 1 else 45
    cov = float(a[2]) if len(a) > 2 else 0.55
    seed = int(a[3]) if len(a) > 3 else 1337
    w = World.open(a[0])
    nc, ad = worldgen.add_mountains(w, max_height=mh, coverage=cov, seed=seed)
    path = w.save(backup=True)
    print("added mountains: %d chunks, %d blocks -> %s (.bak kept)" % (nc, ad, path))


def cmd_report(a):
    # report <save> <out.html>
    from . import analytics
    s = analytics.analyze(a[0])
    open(a[1], "w", encoding="utf-8").write(analytics.report_html(s))
    print("wrote report -> %s  (%d chunks, %d mobs, %d containers, %d spawners)"
          % (a[1], s["chunks"], s["n_mobs"], s["containers"], len(s["spawners"])))


def _parse_block(s, default=(35, 14)):
    if not s:
        return default
    if ":" in s:
        a, b = s.split(":"); return (int(a), int(b))
    return (int(s), 0)


def cmd_pixelart(a):
    # pixelart <image> <out.schematic> [width] [wall|floor] [flat|dither] [depth] [palette]
    from . import artgen
    width = int(a[2]) if len(a) > 2 else 64
    orient = a[3] if len(a) > 3 else "wall"
    dither = (len(a) > 4 and a[4] == "dither")
    depth = int(a[5]) if len(a) > 5 else 1
    palette = a[6] if len(a) > 6 else "smooth"
    shape = artgen.image_to_schematic(a[0], a[1], width=width, orientation=orient,
                                      dither=dither, depth=depth, palette=palette)
    print("pixel art %s (palette=%s depth=%d) -> %s  (import in the viewer with Ctrl+I)"
          % (shape, palette, depth, a[1]))


def cmd_shape(a):
    # shape sphere R | cylinder R H | pyramid BASE  <out.schematic> [id[:meta]] [hollow|solid]
    from . import artgen
    kind, out = a[0], a[1]
    if kind == "sphere":
        sh = artgen.sphere(int(a[2]), out, block=_parse_block(a[3] if len(a) > 3 else ""),
                           hollow=("solid" not in a))
    elif kind == "cylinder":
        sh = artgen.cylinder(int(a[2]), int(a[3]), out,
                             block=_parse_block(a[4] if len(a) > 4 else ""), hollow=("hollow" in a))
    elif kind == "pyramid":
        sh = artgen.pyramid(int(a[2]), out, block=_parse_block(a[3] if len(a) > 3 else "24:0"),
                            solid=("hollow" not in a))
    else:
        print("usage: shape sphere R | cylinder R H | pyramid BASE  <out.schematic> [id[:meta]] [hollow|solid]"); return
    print("%s %s -> %s  (import in the viewer with Ctrl+I)" % (kind, sh, out))


def cmd_preview(a):
    # preview <schematic> <out.png> [scale]
    from . import artgen
    artgen.preview_png(a[0], a[1], scale=int(a[2]) if len(a) > 2 else 6)
    print("preview -> %s" % a[1])


def cmd_scaleschem(a):
    # scaleschem <in.schematic> <out.schematic> <factor>
    from . import artgen
    old, new = artgen.scale_schematic(a[0], a[1], float(a[2]))
    print("scaled %s × %s -> %s  -> %s" % (old, a[2], new, a[1]))


def cmd_text(a):
    # text "<text>" <out.schematic> [height] [id[:meta]] [outline-id[:meta]]
    from . import artgen
    sh = artgen.text_to_schematic(a[0], a[1], height=int(a[2]) if len(a) > 2 else 12,
                                  block=_parse_block(a[3] if len(a) > 3 else "35:15"),
                                  outline=_parse_block(a[4]) if len(a) > 4 else None)
    print("text %r %s -> %s  (import in the viewer with Ctrl+I)" % (a[0], sh, a[1]))


def cmd_totu0(a):
    # totu0 <save|CON> [out_dir]  -- downgrade ANY TU world to load on TU0.
    # Input may be an extracted savegame.dat/folder or a raw STFS CON package.
    from . import convert
    import os
    w = World.open(a[0])
    st = convert.downgrade_to_tu0(w)
    out = a[1] if len(a) > 1 else None
    if out is None and getattr(w, "_con_src", None):     # CON in -> folder out beside it
        base = os.path.splitext(os.path.basename(w._con_src))[0]
        out = os.path.join(os.path.dirname(w._con_src), base + " TU0.bin")
        os.makedirs(out, exist_ok=True)
    path = w.save(out=out, backup=True)
    print("downgraded to TU0 (container v%d -> v2) -> %s (.bak kept)" % (st["src_ver"], path))
    print("  level keys stripped: %d | player tags stripped: %d across %d profiles"
          % (len(st["level_keys"]), st["player_keys"], st["players_cleaned"]))
    print("  entities removed: %d %s | blocks remapped: %d | items remapped: %d"
          % (st["entities_removed"], sorted(st["entity_types"]) or "",
             st["blocks_remapped"], st["items_remapped"]))
    if st["aquatic_converted"] or st["chunk_errors"]:
        print("  Aquatic chunks transcoded: %d (%d errors)"
              % (st["aquatic_converted"], st["chunk_errors"]))


def cmd_obj(a):
    # obj <model.obj> <out.schematic> [size] [id[:meta]|color] [solid]
    from . import voxelize
    size = int(a[2]) if len(a) > 2 else 48
    block = None if (len(a) <= 3 or a[3] in ("color", "solid")) else _parse_block(a[3])
    sh = voxelize.obj_to_schematic(a[0], a[1], size=size, block=block, solid=("solid" in a))
    print("voxelized %s → %s %s  (import in the viewer with Ctrl+I)" % (a[0], sh, a[1]))


def _fixed_out(w, a):
    import os
    out = a[1] if len(a) > 1 else None
    if out is None and getattr(w, "_con_src", None):
        base = os.path.splitext(os.path.basename(w._con_src))[0]
        out = os.path.join(os.path.dirname(w._con_src), base + " fixed.bin")
        os.makedirs(out, exist_ok=True)
    return out


def cmd_repair(a):
    # repair <save|CON> [out]  -- comprehensive crash/hazard scan+fix: orphaned/duplicate/
    # out-of-range tile-entities and broken (NaN/absurd) entities. Blocks untouched.
    w = World.open(a[0])
    st = w.repair_world(log=print)
    total = len(st["orphan_te"]) + st["bad_pos_te"] + st["dup_te"] + st["bad_entities"]
    if not total:
        print("clean — no crash hazards found (orphaned/duplicate tile-entities, broken entities)."); return
    path = w.save(out=_fixed_out(w, a), backup=True,
                    progress=lambda d,t,txt=None: (txt and (t or 0)<=0 and print(txt)))
    print("repaired -> %s (.bak kept)" % path)
    print("  orphaned tile-entities: %d | out-of-range: %d | duplicate: %d | broken entities: %d"
          % (len(st["orphan_te"]), st["bad_pos_te"], st["dup_te"], st["bad_entities"]))
    for tid, x, y, z in st["orphan_te"][:20]:
        print("   orphan %s at (%s,%s,%s)" % (tid, x, y, z))


def cmd_relight(a):
    # relight <save|CON> [out]  -- recompute HeightMap + SkyLight + BlockLight for every
    # chunk (fixes stale lighting from external edits).
    w = World.open(a[0])
    n = w.recompute_lighting(log=print)
    path = w.save(out=_fixed_out(w, a), backup=True,
                    progress=lambda d,t,txt=None: (txt and (t or 0)<=0 and print(txt)))
    print("recomputed lighting for %d chunks -> %s (.bak kept)" % (n, path))


def cmd_iso(a):
    # iso <save|CON> [out.png] [tile]  -- render an isometric 3D portrait of the world
    import os
    from . import iso
    from .view3d.world import World as VW
    dat = a[0]
    if os.path.isdir(dat):
        dat = os.path.join(dat, "savegame.dat")
    vw = VW.load(dat)
    tile = int(a[2]) if len(a) > 2 else 12
    img = iso.render_iso(vw, tile=tile, log=print)
    out = a[1] if len(a) > 1 else os.path.splitext(dat)[0] + "_iso.png"
    img.save(out)
    print("saved isometric portrait %dx%d -> %s" % (img.width, img.height, out))


def cmd_pois(a):
    # pois <save|CON> [type]  -- find structures/POIs (dungeon/loot/stronghold/portal/build/sign)
    from . import poi
    w = World.open(a[0])
    ps = poi.find_pois(w, log=print)
    flt = a[1] if len(a) > 1 else None
    if flt:
        ps = [p for p in ps if p["type"] == flt]
    print("%d point(s) of interest:" % len(ps))
    for p in ps:
        print("  [%-10s] (%6d,%4d,%6d) dim%d  %s" % (p["type"], p["x"], p["y"], p["z"],
                                                     p.get("dim", 0), p["label"]))


def cmd_players(a):
    # players <save|CON>   -- list every player profile in the save
    w = World.open(a[0])
    print("%d player profile(s):" % len(w.players()))
    for pi in w.list_players():
        gt = {0: "survival", 1: "creative", 2: "adventure"}.get(pi["gametype"], "?")
        sp = "%s,%s,%s" % pi["spawn"] if all(v is not None for v in pi["spawn"]) else "world"
        print("  %-22s pos=%s hp=%s items=%d bed=%s %s%s"
              % (pi["xuid"], pi["pos"], pi["health"], pi["items"], sp, gt,
                 "  (active)" if pi["active"] else ""))


def cmd_playerpos(a):
    # playerpos <save|CON> <xuid|all> <x> <y> <z> [out]  -- set Pos + bed spawn
    import os
    w = World.open(a[0]); who = a[1]; x, y, z = int(a[2]), int(a[3]), int(a[4])
    targets = w.players() if who.lower() == "all" else ["players/%s.dat" % who]
    for k in targets:
        w.edit_player(k, pos=(x + 0.5, float(y), z + 0.5), spawn=(x, y, z))
    out = a[5] if len(a) > 5 else _fixed_out(w, a)
    path = w.save(out=out, backup=True)
    print("placed %d player(s) at (%d,%d,%d) -> %s (.bak kept)" % (len(targets), x, y, z, path))


# ---- vendored full converter engine (Xbox360 / PS3 / Windows LCE / Java, any TU) ----
def cmd_xjava(a):
    # xjava <console-save> <out-dir> [platform]   LCE -> Java (every chunk format)
    from .converter import lce_engine as E
    plat = a[2] if len(a) > 2 else "xbox360"
    E.convert_lce_to_java(a[0], plat, a[1], log=print)


def cmd_retarget(a):
    # retarget <console-save> <TU> <out.bin> [platform] [--emu]   rewrite as another title update
    from .converter import lce_engine as E
    src, tu, out = a[0], int(a[1]), a[2]
    plat = a[3] if len(a) > 3 and not a[3].startswith("--") else "xbox360"
    E.convert_console_to_console(src, plat, target_tu=tu, out_path=out,
                                 emulator=("--emu" in a), log=print)


def cmd_towin(a):
    # towin <console-save> <out-dir> [platform]   console -> Windows LCE world folder
    from .converter import lce_engine as E
    plat = a[2] if len(a) > 2 else "xbox360"
    E.convert_console_to_win64(a[0], plat, a[1], log=print)


def cmd_tocon(a):
    # tocon <windows-lce-save> <out> [platform] [--emu]   Windows LCE -> console (.bin / PS3 / emu folder)
    from .converter import lce_engine as E
    plat = a[2] if len(a) > 2 and not a[2].startswith("--") else "xbox360"
    E.convert_win64_to_console(a[0], plat, out_path=a[1],
                               emulator=("--emu" in a), log=print)


def cmd_resign(a):
    # resign <con.bin> [out.bin]   rehash + console-sign an STFS CON package
    from .converter import lce_engine as E
    out = a[1] if len(a) > 1 else a[0].rsplit(".", 1)[0] + ".resigned.bin"
    open(out, "wb").write(E.resign_con(open(a[0], "rb").read()))
    print("resigned -> %s" % out)


_CMDS = {"info": cmd_info, "ls": cmd_ls, "inv": cmd_inv, "block": cmd_block,
         "mob": cmd_mob, "spawner": cmd_spawner, "level": cmd_level,
         "nbt": cmd_nbt, "analyze": cmd_analyze, "convert": cmd_convert,
         "tojava": cmd_tojava, "stfs": cmd_stfs, "reassign": cmd_reassign,
         "unpack": cmd_unpack, "unpackall": cmd_unpackall, "pack": cmd_pack,
         "map": cmd_map, "mapitems": cmd_mapitems, "schem": cmd_schem,
         "flatten": cmd_flatten, "mountains": cmd_mountains, "report": cmd_report,
         "pixelart": cmd_pixelart, "shape": cmd_shape, "preview": cmd_preview,
         "scaleschem": cmd_scaleschem, "text": cmd_text, "obj": cmd_obj,
         "totu0": cmd_totu0, "repair": cmd_repair, "fixorphans": cmd_repair,
         "relight": cmd_relight, "players": cmd_players, "playerpos": cmd_playerpos,
         "pois": cmd_pois, "iso": cmd_iso,
         "xjava": cmd_xjava, "retarget": cmd_retarget, "towin": cmd_towin,
         "tocon": cmd_tocon, "resign": cmd_resign}


def main(argv):
    if not argv or argv[0] not in _CMDS:
        print(__doc__); return 1
    _CMDS[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
