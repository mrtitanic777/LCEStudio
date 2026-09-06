"""LCE Studio -- one Minecraft: Xbox 360 (Legacy Console Edition) save toolkit.

Everything in one package:

    from lce import World
    w = World.open("Save....bin")
    w.inventory_add(264, 64)          # 64 diamonds
    w.set_block(0, 64, 0, 41)         # gold block
    w.add_mob("Giant", -84, 64, 58)
    w.save(backup=True)

    from lce import launch; launch()  # the desktop GUI

Modules:
    lce.codec    - LZX/RLE + region + container codec (the proven low-level engine)
    lce.recover  - format detection, health, container/zip recovery
    lce.stfs     - STFS package reading (tutorial / pre-release builds)
    lce.nbt      - full NBT read/write engine
    lce.inject   - console-exact chunk encode (5-byte trailer) + NBT builders
    lce.world    - high-level editable World / Region / Chunk model
    lce.viz      - map / slice renderers (needs Pillow)
    lce.gui      - the tkinter desktop application
    lce.cli      - `python -m lce ...`
"""
from . import codec, recover, stfs, nbt, inject, world, structure, convert
from .world import World, Region, Chunk

try:
    from . import viz
except Exception:                       # Pillow missing -> engine/CLI still work
    viz = None

__all__ = ["World", "Region", "Chunk", "codec", "recover", "stfs", "nbt",
           "inject", "world", "viz", "launch"]
__version__ = "1.0"


def launch(path=None):
    """Open the desktop GUI (optionally on a save path)."""
    from .gui import Studio
    Studio(path).mainloop()
