# LCE Studio

[![Latest release](https://img.shields.io/github/v/release/mrtitanic777/LCEStudio?sort=semver&label=release)](https://github.com/mrtitanic777/LCEStudio/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/mrtitanic777/LCEStudio/total)](https://github.com/mrtitanic777/LCEStudio/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2064--bit-informational)](https://github.com/mrtitanic777/LCEStudio/releases/latest)

A complete toolkit for **Minecraft: Xbox 360 / Legacy Console Edition (LCE)** save games —
browse, edit, visualize, convert and write LCE saves, and write them back **console-exact**
so they load in-game without freezing.

> ### ⬇ [Download LCEStudio.exe](https://github.com/mrtitanic777/LCEStudio/releases/latest/download/LCEStudio.exe)
> A single ready-to-run executable — no Python or install needed (Windows 64-bit). Or browse all
> [Releases](https://github.com/mrtitanic777/LCEStudio/releases).

It reads **every LCE chunk format** (old-NBT 128/256-tall, tile-storage v8–v11, and the
Aquatic sectioned v12), so it opens worlds from **TU0 through TU75**, and it can convert them
between platforms and title updates and export them to Java.

Everything is one self-contained package (`lce/`). The only runtime dependencies are Python 3
plus Pillow (2D map/renders) and pyglet + numpy (3D viewer). Windows-only conversion features
use bundled DLLs.

## Highlights

- **World Library** — a gallery of every world in your saves folders (thumbnail · name ·
  platform · title update), with search/filter, detail-on-hover (size, seed, dimensions), and
  Open / Convert / Repair on each card.
- **Full editor** — inventory (with a slot grid and searchable block/item **name dropdowns**),
  blocks, entities (searchable mob names), tile-entities, players, spawn, and a raw NBT tree.
- **Global NBT search** — find any tag, id, item, entity or name across the whole save and jump
  to each hit.
- **2D map & inline Isometric view** — top-down / Y-slice maps with markers, plus a whole-world
  3D isometric view you can pan and zoom, right in the app.
- **3D fly-through editor** — a real WASD editor: place/break/fill, box-select, a block atlas,
  and full-structure copy/paste to `.schematic` (with tile-entities).
- **Cross-platform converter** — Xbox 360 ↔ PS3 ↔ Windows LCE, LCE → Java (Anvil), any-title-
  update downgrade/retarget, and STFS `.bin` re-signing.
- **Repair & relight** — fix the orphaned/duplicate tile-entities and broken entities that
  hard-freeze a save; rebuild lighting.
- **Generators** — flatten / mountains, pixel-art & 3D-model → schematic, and ready-to-play
  **challenge worlds** (Skyblock / One-Chunk / Void / Island).
- **World Timelapse** — a snapshot is captured every time you save; scrub the history as
  isometric frames and restore any older version (a visual undo across sessions).

## Run it

**Standalone (no Python needed)** — download **[`LCEStudio.exe`](https://github.com/mrtitanic777/LCEStudio/releases/latest/download/LCEStudio.exe)**
from the [latest release](https://github.com/mrtitanic777/LCEStudio/releases/latest) and run it. A
single portable executable with everything bundled (engine, 2D/3D renderers, converter engine +
DLLs, `lzxc.exe`, textures).
Rebuild it any time with `build_exe.bat` (needs `pyinstaller`), or:
```
python -m PyInstaller --noconfirm LCEStudio.spec
```

**From source** — run **`LCEStudio.bat`**, or:
```
python LCEStudio.py "C:\path\to\Save2026 xxx.bin"
```

**Command line** — `lce-cli.bat`, or `python -m lce <command>`:
```
python -m lce info    "<save>"
python -m lce inv     "<save>" --add 264:64            # 64 diamonds
python -m lce block   "<save>" fill -5 63 -5 5 63 5 20 # glass floor
python -m lce nbt     "<save>" level.dat               # dump an NBT tree
python -m lce xjava   "<save>" <out-dir>               # export to Java (Anvil)
python -m lce towin   "<save>" <out-dir>               # Xbox 360 -> Windows LCE
python -m lce tocon   "<winlce-save>" <out> --emu      # Windows LCE -> console (emulator folder)
python -m lce iso     "<save>" out.png                 # isometric world portrait
```

**Library**
```python
from lce import World
w = World.open("Save....bin")
w.inventory_add(264, 64)
w.set_block(0, 64, 0, 41)
w.add_mob("Giant", -84, 64, 58)
w.save(backup=True)          # console-exact, .bak backup, round-trip verified
```

## The GUI

| Tab | What it does |
|---|---|
| **Library** | gallery of all worlds in your saves folders — thumbnail, name, platform, TU; search; hover for detail; Open / Convert / Repair |
| **Overview** | spawn (editable), player, health, regions, VFS file list |
| **Map** | top-down / Y-slice / **isometric** views; spawn/player/entity markers; world boundary + chunk grid; click to inspect, double-click to edit; 🔍 find structures, 📐 3D portrait, 🎞 timelapse |
| **Inventory** | item table + a Minecraft-style slot grid, searchable item name dropdown; player stats |
| **Blocks** | get / set / fill any block, with a searchable block name dropdown |
| **Entities** | list per chunk or the whole world; add any mob (searchable names) |
| **NBT** | **search the whole save** for anything, then browse the full tag tree of a file |
| **Tools** | flatten / mountains, pixel-art & 3D-model → schematic, **challenge worlds**, one-click content, 3D viewer, world atlas |
| **Convert** | Xbox 360 ↔ PS3 ↔ Windows LCE, → Java, change title update, re-sign a CON, repair / relight |

Every save writes chunks with the mandatory 5-byte segment trailer (or the console freezes at
"Loading spawn area"), backs the original up as `.bak`, and round-trip-verifies first.

## 3D world editor

Tools tab → **Open in 3D fly-through viewer**, or `python -m lce.view3d "<save>"`. A real editor
meshed per-chunk through the same engine, so edits re-mesh instantly:

| | |
|---|---|
| **Move / look** | WASD, Space/Ctrl up-down, mouse look (Esc releases), Shift = faster |
| **Place / Break** | left-click places the selected block; right-click removes the aimed one |
| **Fill / Remove box** | left-drag fills a box; right-drag deletes one (a measuring stick shows N×M×K) |
| **Select a structure** | **J** magic-wands a whole build (bounded by a **K** box to isolate it) |
| **Copy / paste / schematic** | Ctrl+C/V, and Ctrl+E/I export/import `.schematic` (chests & signs travel too) |
| **Block atlas** | **E** opens the palette; **1–9** = hotbar |
| **Export** | **Ctrl+S** writes a console-exact save (.bak backup) |

## Package layout

```
lce/
  world.py       high-level editable World / Region / Chunk model
  codec.py       LZX / RLE / region / container codec (the proven low-level engine)
  inject.py      console-exact chunk encode (5-byte trailer) + NBT builders
  nbt.py         full NBT read/write engine
  names.py       block/item name tables (searchable dropdowns)
  nbtsearch.py   whole-save search index
  recover.py     format detection, health checks, container recovery
  stfs.py        STFS package reading / reassignment
  library.py     World Library scan
  timelapse.py   save-history snapshots + restore
  worldgen.py    flatten / mountains / challenge worlds
  schematic.py   full-structure copy/paste to .schematic (+ tile-entities)
  convert.py     Java <-> LCE (LCE Studio's own converter)
  converter/     vendored cross-platform converter engine (Xbox360/PS3/WinLCE/Java, any TU) + DLLs
  atlas.py viz.py iso.py poi.py analytics.py   maps, isometric render, structure finder, reports
  gui.py cli.py  desktop app + command line
  view3d/        native 3D fly-through viewer (pyglet + OpenGL)
  lzxc.exe       LZX encoder (console-exact compression)
```

See `LCE_MODDING_GUIDE.md` for the save-format and patching deep-dive.

## Credits

- The vendored cross-platform **converter engine** (`lce/converter/`) is built on
  [dtentiion/LCE-Save-Converter](https://github.com/dtentiion/LCE-Save-Converter); credit to its
  authors. Its id tables are Minecraft's own numbering (data only), and its STFS console-signing
  constants derive from the Horizon project.
- Bundled binaries — `xcompress64.dll` (Microsoft XCompress / LZX), `chm_lzx.dll` (CHMLib),
  `LZXDecompression.dll` (LDI), and `lzxc.exe` — are the property of their respective owners and
  are included for interoperability only.

## License

LCE Studio's own source code is released under the **MIT License** — see [LICENSE](LICENSE).
The third-party components listed above retain their own licenses/terms.

*Minecraft is a trademark of Mojang / Microsoft. This is an unofficial, fan-made tool and is not
affiliated with or endorsed by them.*
