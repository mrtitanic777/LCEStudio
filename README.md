# LCE Studio

A complete editor for **Minecraft: Xbox 360 Edition (Legacy Console Edition, TU0)** save games.
Read, edit, visualize and write LCE retail saves — inventory, blocks, entities, tile-entities,
spawn, raw NBT — and write them back **console-exact** so they load without freezing.

Everything is one self-contained package (`lce/`). Nothing else is required except Python 3
and (for the map visualizers) Pillow.

## Run it

**Standalone (no Python needed)** — double-click **`LCEStudio.exe`**. A single portable
executable with everything bundled (engine, 2D map, 3D viewer, `lzxc.exe`, textures).
Rebuild it any time with `build_exe.bat` (needs `pyinstaller`).

**From source** — double-click **`LCEStudio.bat`**, or:
```
python LCEStudio.py "C:\path\to\Save2026 xxx.bin"
```

**Command line** — `lce-cli.bat`, or:
```
python -m lce info    "<save>"
python -m lce inv     "<save>" --add 264:64        # 64 diamonds
python -m lce block   "<save>" fill -5 63 -5 5 63 5 20   # glass floor
python -m lce mob     "<save>" add Giant -84 64 58
python -m lce nbt     "<save>" level.dat           # dump an NBT tree
python -m lce analyze "<container.zip|dir>"        # health / recovery
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
| **Overview** | spawn (editable), player, health, regions, VFS file list |
| **Map** | top-down world map + horizontal Y-slice; spawn/player/entity markers; 54×54 world boundary frame; chunk grid; fit-to-window; click to inspect, double-click to edit a block |
| **Inventory** | item table + a Minecraft-style slot grid; add / remove / clear |
| **Blocks** | get / set / fill any block |
| **Entities** | list per chunk, add any mob (Giant included) |
| **NBT** | browse the full tag tree of any file in the save |
| **Tools** | one-click: give spawner blocks, inject a Giant, build a Giant den, Locked Chest, TNT |

Every save writes chunks with the mandatory 5-byte segment trailer (or the console freezes
at "Loading spawn area"), backs up the original as `.bak`, and round-trip-verifies first.

## Package layout

```
lce/
  codec.py    LZX / RLE / region / container codec  (the proven low-level engine)
  recover.py  format detection, health checks, zip/container recovery
  stfs.py     STFS package reading (tutorial / pre-release builds)
  nbt.py      full NBT read/write engine
  inject.py   console-exact chunk encode (5-byte trailer) + NBT builders
  world.py    high-level editable World / Region / Chunk model
  viz.py      2D map / slice renderers (Pillow)
  gui.py      the tkinter desktop application
  cli.py      command line
  view3d/     native 3D fly-through viewer (pyglet + OpenGL, textured mesher)
  lzxc.exe    XexTool-RE LZX encoder (console-exact compression)
```

## 3D world editor

Tools tab → **Open in 3D fly-through viewer**, or from the command line:
```
python -m lce.view3d "<save>" [--radius 12] [--shot out.png]
```
A real editor, not just a viewer — meshed per-chunk through the same engine so edits
re-mesh instantly:

| | |
|---|---|
| **Move / look** | WASD, Space/Ctrl up-down, mouse look (click to capture, Esc releases), Shift = faster |
| **Place** | Left-click — put the selected block on the face you're aiming at |
| **Fill box** | Left-drag — hold, re-aim, release: fills the box with the block |
| **Break** | Right-click — remove the aimed block |
| **Remove box** | Right-drag — the *measuring stick*: a box shows N×M×K, released → deleted |
| **Block atlas** | **E** — open the palette, click a block to select; **1–9** = hotbar |
| **Export** | **Ctrl+S** — write console-exact save (.bak backup) to load in-game |
| **Quit** | Q or close |

Needs `pyglet` and `numpy`.

Dependencies: Python 3, `Pillow` (2D map), `pyglet` + `numpy` (3D view). See `requirements.txt`.

## Notes

- LCE TU0 finite worlds pre-generate only a **25×25-chunk spawn square**; the rest of the
  **54×54 boundary** streams in as you explore. The Map tab frames your generated area inside
  the true world square.
- Giants need the **"Stable Giant (2×)" runtime patch** (Nexia360 `.patch.toml`) to load
  without crashing — the editor injects the mob; the patch keeps it stable.

See `LCE_MODDING_GUIDE.md` for the format and patching deep-dive.
