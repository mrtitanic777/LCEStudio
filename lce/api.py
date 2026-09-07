"""LCEStudio API spine — one clean, documented surface over every subsystem.

The whole toolkit behind a single object. Open a save and reach edit, render, search,
convert, generate and analyse through domain namespaces that delegate to the existing
modules (world, atlas, iso, nbtsearch, mapart, poi, schematic, worldgen, timelapse,
waypoints, converter). Heavy dependencies (Pillow, pyglet, the converter engine) are
imported lazily, so importing this module is cheap.

    import lce
    s = lce.open("Save....bin")
    s.blocks.set(0, 64, 0, 41)              # gold block
    s.inventory.add(264, 64)               # 64 diamonds
    s.players.teleport(s.players.first(), 100, 70, -40)
    img = s.render.map()                   # PIL top-down map
    hits = s.search.find("diamond sword")
    s.maps.paint("logo.png")               # image -> in-game map
    s.save(backup=True)

Stateless helpers live on the module: `lce.open`, `lce.library`, `lce.describe`,
`lce.convert_title_update`, `lce.convert_platform`, `lce.convert_java`. Every capability
is also discoverable through `lce.registry`.
"""
from . import registry as R
from .registry import Param


# ---------------------------------------------------------------- domain facades
class _Blocks:
    def __init__(self, s): self._s = s
    def get(self, x, y, z, nether=False): return self._s.world.get_block(x, y, z, nether)
    def data(self, x, y, z, nether=False): return self._s.world.get_block_data(x, y, z, nether)
    def set(self, x, y, z, bid, data=0, nether=False):
        return self._s.world.set_block(x, y, z, bid, data, nether)
    def fill(self, x1, y1, z1, x2, y2, z2, bid, data=0, nether=False):
        return self._s.world.fill(x1, y1, z1, x2, y2, z2, bid, data, nether)


class _Inventory:
    def __init__(self, s): self._s = s
    def items(self): return self._s.world.inventory()
    def add(self, item_id, count=1, damage=0): return self._s.world.inventory_add(item_id, count, damage)
    def clear(self): return self._s.world.inventory_clear()


class _Players:
    def __init__(self, s): self._s = s
    def list(self): return self._s.world.list_players()
    def keys(self): return self._s.world.players()
    def first(self): ks = self._s.world.players(); return ks[0] if ks else None
    def spawn(self): return self._s.world.get_spawn()
    def set_spawn(self, x, y, z): return self._s.world.set_spawn(x, y, z)
    def add(self, *a, **k): return self._s.world.add_player(*a, **k)
    def remove(self, key): return self._s.world.remove_player(key)
    def rename(self, key, xuid): return self._s.world.rename_player(key, xuid)
    def edit(self, key, **fields): return self._s.world.edit_player(key, **fields)
    def teleport(self, key, x, y, z, dimension=None, surface=False):
        w = self._s.world
        if surface:
            for yy in range(min(126, 250), 0, -1):
                if w.get_block(int(x), yy, int(z)) not in (0, None):
                    y = yy + 1; break
        return w.edit_player(key, pos=(x + 0.5, float(y), z + 0.5), dimension=dimension)


class _Entities:
    def __init__(self, s): self._s = s
    def add_mob(self, name, x, y, z, **k): return self._s.world.add_mob(name, x, y, z, **k)
    def add_spawner(self, mob, x, y, z, **k): return self._s.world.add_spawner(mob, x, y, z, **k)
    def list(self, cx, cz, nether=False): return self._s.world.entities(cx, cz, nether)


class _Search:
    """Whole-save NBT search (lce.nbtsearch), with a cached index."""
    def __init__(self, s): self._s = s; self._index = None
    @property
    def indexed(self): return self._index is not None
    def index(self, log=None):
        if self._index is None:
            from . import nbtsearch
            self._index = nbtsearch.build_index(self._s.world, log=log)
        return self._index
    def find(self, query):
        from . import nbtsearch
        return nbtsearch.filter_index(self.index(), query)
    def invalidate(self): self._index = None


class _Render:
    """Renderers (need Pillow). Return PIL images."""
    def __init__(self, s): self._s = s
    def _vw(self):
        if self._s._vw is None:
            from .view3d.world import World as VW
            self._s._vw = VW.load(self._s.path)
        return self._s._vw
    def map(self, shade=True, water_depth=True):
        from . import atlas
        return atlas.render_world(self._vw(), shade=shade, water_depth=water_depth)
    def slice(self, y, shade=False):
        from . import atlas
        return atlas.render_slice(self._vw(), y, shade=shade)
    def iso(self, tile=8, log=None):
        from . import iso
        return iso.render_iso(self._vw(), tile=tile, log=log)


class _Maps:
    """In-game map items (lce.mapart / lce.atlas)."""
    def __init__(self, s): self._s = s
    def list(self):
        from . import mapart
        return mapart.list_maps(self._s.world)
    def render(self, dat_bytes):
        from . import atlas
        return atlas.render_map_item(dat_bytes)
    def paint(self, image, dither=True, scale=0, replace=None):
        """Paint an image (path or PIL.Image) onto a NEW map, or `replace` an existing
        one (its data/map_N.dat name). Returns the map's VFS name. Persists on save."""
        from . import mapart
        from PIL import Image as _I
        img = image if hasattr(image, "convert") else _I.open(image)
        if replace:
            name = replace if replace.startswith("data/") else "data/" + replace
            dat = mapart.image_to_map_dat(img, dither=dither,
                                          existing=self._s.world._filedata.get(name), scale=scale)
            return mapart.put_map(self._s.world, name, dat)
        dat = mapart.image_to_map_dat(img, dither=dither, scale=scale)
        return mapart.add_map(self._s.world, dat)


class _Structures:
    def __init__(self, s): self._s = s
    def find(self, log=None):
        from . import poi
        return poi.find_pois(self._s.world, log=log)
    def extract(self, x1, y1, z1, x2, y2, z2):
        from . import schematic
        return schematic.extract(self._s.world, x1, y1, z1, x2, y2, z2)


class _Generate:
    def __init__(self, s): self._s = s
    def flatten(self, keep_builds=True, log=None):
        from . import worldgen
        return worldgen.flatten(self._s.world, keep_builds=keep_builds, log=log or (lambda *a: None))
    def mountains(self, height=45, log=None):
        from . import worldgen
        return worldgen.add_mountains(self._s.world, height, log=log or (lambda *a: None))
    def challenge(self, kind="skyblock", log=None):
        from . import worldgen
        return worldgen.generate_challenge(self._s.world, kind, log=log or (lambda *a: None))


class _Timelapse:
    def __init__(self, s): self._s = s
    def snapshots(self):
        from . import timelapse
        return timelapse.list_snapshots(self._s.path)
    def snapshot(self):
        from . import timelapse
        return timelapse.snapshot(self._s.path)
    def frame(self, index, tile=8):
        from . import timelapse
        return timelapse.frame(self._s.path, index, tile=tile)
    def restore(self, index):
        from . import timelapse
        return timelapse.restore(self._s.path, index)


class _Waypoints:
    def __init__(self, s): self._s = s
    def load(self):
        from . import waypoints
        return waypoints.load(self._s.path)
    def store(self, wps):
        from . import waypoints
        return waypoints.store(self._s.path, wps)


class _Info:
    def __init__(self, s): self._s = s
    def title_update(self, platform="xbox360"):
        from . import library
        return library.accurate_tu(self._s.path, platform)
    def describe(self, platform="xbox360"):
        from . import library
        return library.describe(self._s.path, platform)
    def analyze(self):
        from . import analytics
        return analytics.analyze(self._s.world)
    def report_html(self):
        from . import analytics
        return analytics.report_html(self._s.world)


# ---------------------------------------------------------------- Session
class Session:
    """An open save plus every domain namespace. Create with `lce.open(path)`."""

    def __init__(self, world, path=None, meta=None):
        self.world = world
        self.path = path
        self.meta = meta or {}
        self._vw = None                       # cached view3d world for renders
        self.blocks = _Blocks(self)
        self.inventory = _Inventory(self)
        self.players = _Players(self)
        self.entities = _Entities(self)
        self.search = _Search(self)
        self.render = _Render(self)
        self.maps = _Maps(self)
        self.structures = _Structures(self)
        self.generate = _Generate(self)
        self.timelapse = _Timelapse(self)
        self.waypoints = _Waypoints(self)
        self.info = _Info(self)

    @classmethod
    def open(cls, path):
        from .world import World
        return cls(World.open(path), path=path)

    @property
    def name(self):
        return (self.meta.get("name")
                or (self.world.level.get_value("LevelName") if self.world else None) or "world")

    def save(self, out=None, backup=True, **kw):
        self.search.invalidate(); self._vw = None
        return self.world.save(out=out, backup=backup, **kw)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def __repr__(self): return "<lce.Session %r>" % (self.path,)


# ---------------------------------------------------------------- stateless API
def open(path):
    """Open a save (any LCE shape) and return a Session — the API spine's front door."""
    return Session.open(path)


def library_scan(folders, log=None, enrich=False):
    """Scan saves folders into world entries (lce.library). enrich=True also reads the
    accurate title update + duplicate fingerprints."""
    from . import library as L
    worlds = L.scan(list(folders), log=log)
    if enrich:
        L.enrich(worlds, log=log)
    return worlds


def describe(path, platform="xbox360"):
    from . import library as L
    return L.describe(path, platform)


def convert_title_update(src, target_tu, platform="xbox360", out_path=None,
                         emulator=False, log=None):
    """Retarget a console / Windows-LCE save to any TU0-75 (lce.converter engine)."""
    from .converter import lce_engine as E
    return E.convert_console_to_console(src, platform, target_tu=target_tu,
                                        out_path=out_path, emulator=emulator, log=log)


def convert_platform(src, to, platform="xbox360", out_path=None, log=None, **kw):
    """Convert between platforms. `to` is 'windows_lce', 'xbox360' or 'ps3'."""
    from .converter import lce_engine as E
    if to == "windows_lce":
        return E.convert_console_to_win64(src, platform, out_dir=out_path, log=log, **kw)
    return E.convert_win64_to_console(src, to, out_path=out_path, log=log, **kw)


def convert_java(src, platform="xbox360", out_dir=None, java_version=None, log=None):
    """Export an LCE world to a Java (Anvil) world folder (lce.converter engine)."""
    from .converter import lce_engine as E
    return E.convert_lce_to_java(src, platform, out_dir=out_dir, java_version=java_version, log=log)


# ---------------------------------------------------------------- core capabilities
# Registered here so the GUI/CLI/API all discover the same feature set. `session` is an
# open Session for scope="session"; `path`/`src` for scope="path".
def _register_core():
    if R.has("edit.flatten"):
        return

    @R.capability("convert.title_update", "Change title update", "convert",
                  "Retarget a console or Windows-LCE save to any TU0-75.", scope="path",
                  cli="totu", result="path",
                  params=[Param("target_tu", "int", 19, "Target title update (0-75)"),
                          Param("platform", "choice", "xbox360", "Source platform",
                                choices=("xbox360", "ps3", "windows_lce")),
                          Param("emulator", "bool", False, "Write an emulator folder")])
    def _cap_tu(path=None, src=None, target_tu=19, platform="xbox360", emulator=False,
                out_path=None, log=None, **kw):
        return convert_title_update(path or src, target_tu, platform, out_path, emulator, log)

    @R.capability("convert.java", "Export to Java", "convert",
                  "Write a Java Edition (Anvil) world folder.", scope="path",
                  cli="xjava", result="path",
                  params=[Param("java_version", "str", None, "Target Java version (blank = auto)"),
                          Param("platform", "choice", "xbox360", "Source platform",
                                choices=("xbox360", "ps3", "windows_lce"))])
    def _cap_java(path=None, src=None, java_version=None, platform="xbox360",
                  out_dir=None, log=None, **kw):
        return convert_java(path or src, platform, out_dir, java_version, log)

    @R.capability("render.map", "Top-down world map", "render",
                  "Render a satellite-style PNG of the whole world.", result="image")
    def _cap_map(session=None, **kw):
        return session.render.map()

    @R.capability("render.iso", "Isometric portrait", "render",
                  "Render a 3D isometric portrait of the world.", result="image",
                  params=[Param("tile", "int", 8, "Block size in px")])
    def _cap_iso(session=None, tile=8, **kw):
        return session.render.iso(tile=tile)

    @R.capability("tools.paint_map", "Paint an image onto a map", "tools",
                  "Turn any image into an in-game map item.", result="text",
                  params=[Param("image", "path", None, "Image file", required=True),
                          Param("dither", "bool", True, "Dither to the palette")])
    def _cap_paint(session=None, image=None, dither=True, scale=0, **kw):
        return session.maps.paint(image, dither=dither, scale=scale)

    @R.capability("analyze.structures", "Find structures & loot", "analyze",
                  "Scan for dungeons, chests, portals, strongholds, signs and builds.",
                  result="data")
    def _cap_poi(session=None, **kw):
        return session.structures.find()

    @R.capability("analyze.search", "Search the whole save", "analyze",
                  "Find any tag, id, item, entity or name across the save.", result="data",
                  params=[Param("query", "str", "", "Search text", required=True)])
    def _cap_search(session=None, query="", **kw):
        return session.search.find(query)

    @R.capability("analyze.report", "World analytics report", "analyze",
                  "An HTML report of the world's contents.", result="text")
    def _cap_report(session=None, **kw):
        return session.info.report_html()

    @R.capability("generate.challenge", "Challenge world", "generate",
                  "Wipe to void and build Skyblock / One-Chunk / Void / Island.",
                  result="world",
                  params=[Param("kind", "choice", "skyblock", "Challenge kind",
                                choices=("skyblock", "one-chunk", "void", "island"))])
    def _cap_challenge(session=None, kind="skyblock", **kw):
        return session.generate.challenge(kind)

    @R.capability("edit.flatten", "Flatten to sea level", "edit",
                  "Cut terrain above the water line, keeping builds.", result="world")
    def _cap_flatten(session=None, keep_builds=True, **kw):
        return session.generate.flatten(keep_builds=keep_builds)


_register_core()
