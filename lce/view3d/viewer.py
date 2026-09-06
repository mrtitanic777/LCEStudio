"""Fast, streaming 3D world viewer/editor for an LCE save.

A revamp for speed + a readable UI:
  * chunk meshes STREAM in (render-distance bounded, nearest-first, time-budgeted)
    so the window appears at once instead of freezing for a minute-plus;
  * frustum + distance culling means only what you can see is drawn;
  * the raycast that highlights the aimed block runs once per frame, not per draw;
  * the HUD is dark panels + light text (readable), with a real crosshair, a live
    FPS/coords readout, and a leak-free block palette;
  * distance fog fades the far edge into the sky.

Controls
    Move        W A S D , Space up , Ctrl down , hold Shift = faster
    Look        move the mouse (click once to capture; Esc releases)
    Place       Left-click  -> put the selected block on the face you're aiming at
    Fill box    Left-drag   -> hold, re-aim, release: fills the box
    Break       Right-click -> remove the block you're aiming at
    Remove box  Right-drag  -> released -> the whole box is deleted
    Pick block  Middle-click-> eyedrop the block you're aiming at into your hand
    Palette     E           -> open the block atlas, click a block to select it
    Hotbar      1..9        -> quick-select from the palette
    Fly speed   Mouse wheel -> faster / slower flight
    Teleport    Home spawn · P last-player-pos · T then type "X Y Z" + Enter
    Undo/Redo   Ctrl+Z / Ctrl+Y
    Copy/Paste  K sets the two selection corners · Ctrl+C copy · Ctrl+V paste at aim
    Wand select J           -> auto-select the whole built structure you're aiming at
    Schematic   Ctrl+E export the selection to a .schematic · Ctrl+I import one at aim
    Find block  F           -> highlight every nearby copy of the block in your hand
    Entities    M           -> markers on mobs (red) + chests/spawners/signs (cyan)
    Lighting    B           -> cycle normal / fullbright / night
    Chunk grid  G           -> toggle chunk-border overlay
    Render dist [  /  ]      -> fewer / more chunks
    Fullscreen  F11
    Export      Ctrl+S      -> write console-exact save (.bak backup) to load in-game
    Quit        Alt+F4  (or the window's X) — no key closes the viewer

    python -m lce.view3d "<save>"  [--radius N]  [--shot out.png]
"""
import ctypes
import math
import os
import sys
import time

import numpy as np
import pyglet
from PIL import Image
from pyglet.gl import *
from pyglet.graphics.shader import Shader, ShaderProgram
from pyglet.math import Mat4

from .world import World, CHUNK_X, CHUNK_Z, CHUNK_Y
from . import mesher, blocks

_ATLAS_PATH = os.path.join(os.path.dirname(__file__), "textures", "terrain.png")

# variant names for the metadata-bearing blocks
_WOOL = ["White", "Orange", "Magenta", "Light Blue", "Yellow", "Lime", "Pink", "Gray",
         "Light Gray", "Cyan", "Purple", "Blue", "Brown", "Green", "Red", "Black"]
_WOOD = ["Oak", "Spruce", "Birch"]
_SLABMAT = ["Stone", "Sandstone", "Wood", "Cobblestone"]


def _make_palette():
    """Every placeable TU0 (Beta 1.6.6) block as an (id, data) pair, INCLUDING all
    metadata variants: 16 wool colours, 3 wood/leaf/sapling types, 4 slab materials,
    plus water and lava. (0,0) = eraser (air)."""
    p = [(0, 0)]
    solids = [1, 4, 48, 45, 98, 24, 43, 5, 47, 58, 54, 61, 23, 25, 84, 46, 19, 22,
              41, 42, 57, 89, 2, 3, 60, 12, 13, 82, 88, 87, 7, 20, 79, 80, 81, 86,
              91, 52, 49, 14, 15, 16, 21, 56, 73]
    p += [(b, 0) for b in solids]
    p += [(44, m) for m in range(4)]                # slabs: stone/sandstone/wood/cobble
    p += [(17, m) for m in range(3)]                # logs
    p += [(18, m) for m in range(3)]                # leaves
    p += [(6, m) for m in range(3)]                 # saplings
    p += [(53, 0), (67, 0), (85, 0), (65, 0), (64, 0), (66, 0), (55, 0)]
    p += [(35, m) for m in range(16)]               # all 16 wool colours
    p += [(37, 0), (38, 0), (39, 0), (40, 0), (31, 1), (31, 2), (32, 0), (30, 0), (83, 0), (78, 0)]
    p += [(50, 0), (76, 0), (8, 0), (10, 0)]        # torch, redstone torch, water, lava
    return p


PALETTE = _make_palette()


def _blkname(cur):
    """Readable name for an (id, data) selection, with the colour/variant."""
    bid, meta = cur if isinstance(cur, tuple) else (cur, 0)
    if bid == 0:
        return "eraser (air)"
    base = blocks.name(bid)
    if bid == 35:
        return "%s Wool" % _WOOL[meta & 15]
    if bid in (17, 18, 6) and (meta & 3) < 3:
        return "%s %s" % (_WOOD[meta & 3], base)
    if bid in (43, 44) and (meta & 7) < 4:
        return "%s %s" % (_SLABMAT[meta & 3], base)
    return base

_SKY = (0.52, 0.70, 0.92)                      # horizon/sky + fog colour
RENDER_DIST = 12                               # default chunks meshed/drawn around the camera
MAX_RENDER_DIST = 64                           # manual/auto cap ([ ] key); covers most worlds whole
AUTO_FIT_DIST = 48                             # upper bound when auto-fitting to a world on load
MESH_BUDGET_S = 0.010                          # per-frame time budget for streaming meshes


def _load_atlas(path):
    img = Image.open(path).convert("RGBA")
    data = np.frombuffer(img.tobytes(), np.uint8)
    tex = GLuint(); glGenTextures(1, tex); glBindTexture(GL_TEXTURE_2D, tex)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img.width, img.height, 0,
                 GL_RGBA, GL_UNSIGNED_BYTE, data.ctypes.data_as(ctypes.c_void_p))
    return tex


_VERT = """#version 330 core
layout(location = 0) in vec3 in_pos;
layout(location = 1) in vec3 in_col;
layout(location = 2) in vec2 in_uv;
uniform mat4 u_mvp;
uniform vec3 u_eye;
out vec3 v_col; out vec2 v_uv; out float v_dist;
void main() {
    v_col = in_col; v_uv = in_uv;
    v_dist = length(in_pos - u_eye);
    gl_Position = u_mvp * vec4(in_pos, 1.0);
}
"""
_FRAG = """#version 330 core
in vec3 v_col; in vec2 v_uv; in float v_dist;
uniform sampler2D atlas;
uniform vec3 u_fog;
uniform float u_fog0;
uniform float u_fog1;
uniform float u_bright;
out vec4 frag;
void main() {
    vec4 t = texture(atlas, v_uv);
    if (t.a < 0.4) discard;
    vec3 c = clamp(t.rgb * v_col * u_bright, 0.0, 1.0);
    float f = clamp((v_dist - u_fog0) / (u_fog1 - u_fog0), 0.0, 1.0);
    frag = vec4(mix(c, u_fog, f), 1.0);
}
"""
_FLAT_V = """#version 330 core
layout(location = 0) in vec3 in_pos;
uniform mat4 u_mvp;
void main() { gl_Position = u_mvp * vec4(in_pos, 1.0); }
"""
_FLAT_F = """#version 330 core
uniform vec4 u_rgba;
out vec4 frag;
void main() { frag = u_rgba; }
"""


def _look_at(eye, fwd, up=(0.0, 1.0, 0.0)):
    f = np.array(fwd, np.float64); f /= np.linalg.norm(f)
    s = np.cross(f, up); s /= np.linalg.norm(s)
    u = np.cross(s, f)
    e = np.array(eye, np.float64)
    return Mat4(s[0], u[0], -f[0], 0.0, s[1], u[1], -f[1], 0.0,
                s[2], u[2], -f[2], 0.0, -s.dot(e), -u.dot(e), f.dot(e), 1.0)


def _buffer(target, arr, usage=GL_STATIC_DRAW):
    b = GLuint(); glGenBuffers(1, b); glBindBuffer(target, b)
    glBufferData(target, arr.nbytes, arr.ctypes.data_as(ctypes.c_void_p), usage)
    return b


def _tile_uv(bid, meta=0, kind=1):
    t = int(mesher._TILE_LUT[bid, meta, kind])
    col, row = t % 16, t // 16
    return col / 16.0, row / 16.0, (col + 1) / 16.0, (row + 1) / 16.0


def _frustum_planes(mvp):
    """Six (a,b,c,d) planes from a column-major MVP (Gribb-Hartmann). A point is
    inside when a*x+b*y+c*z+d >= 0 for every plane."""
    m = list(mvp)
    r0 = (m[0], m[4], m[8], m[12]); r1 = (m[1], m[5], m[9], m[13])
    r2 = (m[2], m[6], m[10], m[14]); r3 = (m[3], m[7], m[11], m[15])
    def add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3])
    def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2], a[3] - b[3])
    return (add(r3, r0), sub(r3, r0), add(r3, r1), sub(r3, r1), add(r3, r2), sub(r3, r2))


def _aabb_in_frustum(planes, x0, y0, z0, x1, y1, z1):
    for a, b, c, d in planes:
        px = x1 if a >= 0 else x0
        py = y1 if b >= 0 else y0
        pz = z1 if c >= 0 else z0
        if a * px + b * py + c * pz + d < 0.0:
            return False
    return True


class Editor(pyglet.window.Window):
    def __init__(self, world, savefile=None, radius=None, progress=None, holder=None, **kw):
        config = pyglet.gl.Config(major_version=3, minor_version=3, depth_size=24,
                                  double_buffer=True, sample_buffers=1, samples=4)
        try:
            super().__init__(1280, 800, "LCE World Viewer", resizable=True, config=config, **kw)
        except pyglet.window.NoSuchConfigException:
            config = pyglet.gl.Config(major_version=3, minor_version=3, depth_size=24, double_buffer=True)
            super().__init__(1280, 800, "LCE World Viewer", resizable=True, config=config, **kw)
        self.loading = world is None
        self._progress = progress if progress is not None else {}
        self._holder = holder if holder is not None else {}
        self.world = world if world is not None else World()   # placeholder until loaded
        if self.loading:
            self.world.name = "Loading…"
        self.mesh_prog = ShaderProgram(Shader(_VERT, "vertex"), Shader(_FRAG, "fragment"))
        self.flat_prog = ShaderProgram(Shader(_FLAT_V, "vertex"), Shader(_FLAT_F, "fragment"))
        self.u_mvp = glGetUniformLocation(self.mesh_prog.id, b"u_mvp")
        self.u_eye = glGetUniformLocation(self.mesh_prog.id, b"u_eye")
        self.u_fog = glGetUniformLocation(self.mesh_prog.id, b"u_fog")
        self.u_fog0 = glGetUniformLocation(self.mesh_prog.id, b"u_fog0")
        self.u_fog1 = glGetUniformLocation(self.mesh_prog.id, b"u_fog1")
        self.u_bright = glGetUniformLocation(self.mesh_prog.id, b"u_bright")
        self.fu_mvp = glGetUniformLocation(self.flat_prog.id, b"u_mvp")
        self.fu_rgba = glGetUniformLocation(self.flat_prog.id, b"u_rgba")
        glClearColor(*_SKY, 1.0)
        glEnable(GL_DEPTH_TEST); glEnable(GL_CULL_FACE)
        glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        self.render_dist = radius or RENDER_DIST
        self._dist_user_set = radius is not None      # explicit radius -> skip auto-fit
        self.atlas = _load_atlas(_ATLAS_PATH)
        self.chunks_gl = {}                      # (cx,cz) -> (vao, nidx, [bufs], y0, y1)
        self.pending = []                        # chunk keys queued to mesh (nearest first)
        self.queued = set()
        self.tris = 0
        self._last_ccx = self._last_ccz = None

        self._wire_vao = GLuint(); glGenVertexArrays(1, self._wire_vao)
        glBindVertexArray(self._wire_vao)
        self._wire_buf = GLuint(); glGenBuffers(1, self._wire_buf)
        glBindBuffer(GL_ARRAY_BUFFER, self._wire_buf)
        glEnableVertexAttribArray(0); glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, 0)
        glBindVertexArray(0)

        self._build_load_screen()
        self.pos = list(self._safe_start())
        self.yaw, self.pitch = math.radians(45), -0.35
        self.keys = pyglet.window.key.KeyStateHandler(); self.push_handlers(self.keys)
        self.captured = False
        self.cur = (1, 0)                          # (block id, data/meta)
        self.palette_open = False
        self.drag_btn = None; self.dragA = None; self.dragB = None
        self.aim_hit = self.aim_adj = None
        self._dirty = False
        self.flash = ""; self.flash_t = 0.0
        self.toast = ""; self.toast_t = 0.0; self.toast_good = True
        self._fps = 0.0; self._fps_t = time.time(); self._fps_n = 0
        self._pal_gl = None
        # --- feature state ---
        self.fly_speed = 22.0                    # base m/s (mouse-wheel adjustable)
        self.bright_mode = 0                      # 0 normal · 1 fullbright · 2 night
        self.show_borders = False                 # chunk-border overlay (G)
        self.show_entities = False                 # mob/tile-entity markers (M)
        self.undo_stack = []; self.redo_stack = []
        self.sel_a = self.sel_b = None            # copy/paste selection corners (K)
        self.clipboard = None                     # (dx,dz-extent, np array of ids)
        self.clipboard_tes = []                   # tile-entities (relative coords) for Ctrl+V paste
        self._wand = None                         # (blocks, data) of a magic-wand structure
        self._wand_origin = None                  # its min-corner world coords (x0,y0,z0)
        self._wand_cells = None                   # the flooded cell set (for TE masking)
        self.locate_id = None; self.locate_cells = []   # 'find a block' highlights (F)
        self.typing = None                        # coord-entry string, or None
        self._build_hud()
        self._update_title()

    # ------------------------------------------------------------ loading screen
    def _build_load_screen(self):
        import pyglet.shapes as S
        self.load_batch = pyglet.graphics.Batch()
        w, h = self.width, self.height
        self._ls_bg = S.Rectangle(0, 0, w, h, color=(16, 18, 24, 255), batch=self.load_batch)
        self._ls_bar_bg = S.Rectangle(w // 2 - 220, h // 2 - 14, 440, 22, color=(40, 44, 54, 255),
                                      batch=self.load_batch)
        self._ls_bar = S.Rectangle(w // 2 - 218, h // 2 - 12, 4, 18, color=(90, 170, 250, 255),
                                   batch=self.load_batch)
        self._ls_title = pyglet.text.Label("LCE World Viewer", x=w // 2, y=h // 2 + 46,
                                           anchor_x="center", font_size=20,
                                           color=(235, 240, 250, 255), batch=self.load_batch)
        self._ls_sub = pyglet.text.Label("opening save…", x=w // 2, y=h // 2 + 20,
                                         anchor_x="center", font_size=11,
                                         color=(160, 172, 190, 255), batch=self.load_batch)

    def _draw_load_screen(self):
        p = self._progress
        total = max(1, p.get("total", 1)); done = p.get("done", 0)
        frac = 0.05 if p.get("phase") == "opening save" else min(1.0, done / total)
        self._ls_bar.width = max(4, int(436 * frac))
        nm = self.world.name if self.world.name not in ("Loading…", "world") else ""
        self._ls_sub.text = "%s%s" % (p.get("phase", "loading"),
                                      ("   ·   %s" % nm) if nm else "")
        glDisable(GL_DEPTH_TEST)
        glClearColor(0.06, 0.07, 0.09, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self.load_batch.draw()
        glClearColor(*_SKY, 1.0)

    def _finalize_load(self, w):
        self.world = w
        self.pos = list(self._safe_start())
        self._last_ccx = self._last_ccz = None
        self.loading = False
        self._fit_render_dist()
        self._update_title()

    def _fit_render_dist(self):
        """Expand render distance so the WHOLE world is visible at once instead of
        culling distant structures past the default 12 chunks. The per-frame draw is
        still frustum-culled to what's on screen, so a large render distance is cheap
        (chunks just mesh once and stay). Skipped if the user set a distance already."""
        if getattr(self, "_dist_user_set", False):
            return
        ch = self.world.chunks
        if not ch:
            return
        xs = [c[0] for c in ch]; zs = [c[1] for c in ch]
        span = max(max(xs) - min(xs), max(zs) - min(zs))     # full extent, so it stays
        self.render_dist = max(RENDER_DIST, min(AUTO_FIT_DIST, span + 3))  # whole world covered

    # ------------------------------------------------------------ start pos
    def _safe_start(self):
        """Put the camera just outside the build, not embedded in a wall: rise to
        the tallest solid near spawn, step back, and look toward it."""
        sx, sy, sz = self.world.spawn
        top = sy
        for dx in range(-6, 7, 2):
            for dz in range(-6, 7, 2):
                for y in range(min(CHUNK_Y - 1, sy + 40), 0, -1):
                    if self.world.block(sx + dx, y, sz + dz) != 0:
                        top = max(top, y); break
        return (float(sx) - 14.0, float(top) + 10.0, float(sz) - 14.0)

    # ------------------------------------------------------------ HUD (pyglet)
    def _build_hud(self):
        self.ui = pyglet.graphics.Batch()
        self.ui_top = pyglet.graphics.Group(order=0)
        self.ui_txt = pyglet.graphics.Group(order=1)
        import pyglet.shapes as S
        panel = (18, 20, 26)
        self._p_info = S.BorderedRectangle(10, self.height - 158, 336, 148, border=1,
                                           color=(*panel, 205), border_color=(120, 140, 175, 205),
                                           batch=self.ui, group=self.ui_top)
        self._p_help = S.Rectangle(0, 0, self.width, 46, color=(*panel, 200),
                                   batch=self.ui, group=self.ui_top)
        self._p_flash = S.Rectangle(10, self.height - 190, 10, 30, color=(30, 34, 44, 0),
                                    batch=self.ui, group=self.ui_top)
        self.l_title = pyglet.text.Label("", x=22, y=self.height - 26, font_size=13,
                                         color=(235, 240, 250, 255), batch=self.ui, group=self.ui_txt)
        self.l_info = pyglet.text.Label("", x=22, y=self.height - 52, font_size=10, multiline=True,
                                        width=316, color=(200, 210, 225, 255),
                                        batch=self.ui, group=self.ui_txt)
        self.l_help = pyglet.text.Label("", x=12, y=40, font_size=9, multiline=True,
                                        width=self.width - 20, color=(205, 212, 225, 255),
                                        batch=self.ui, group=self.ui_txt)
        self.l_flash = pyglet.text.Label("", x=26, y=self.height - 175, font_size=11,
                                         color=(255, 255, 255, 0), batch=self.ui, group=self.ui_txt)
        # crosshair (two bars) drawn as shapes
        cx, cy = self.width // 2, self.height // 2
        self._cross = [S.Rectangle(cx - 9, cy - 1, 18, 2, color=(255, 255, 255, 200),
                                   batch=self.ui, group=self.ui_txt),
                       S.Rectangle(cx - 1, cy - 9, 2, 18, color=(255, 255, 255, 200),
                                   batch=self.ui, group=self.ui_txt)]
        # toast (save banner) — its own batch so we can show/hide cleanly
        self.toast_batch = pyglet.graphics.Batch()
        self._toast_bg = S.Rectangle(0, self.height - 96, self.width, 44, color=(20, 120, 55, 0),
                                     batch=self.toast_batch)
        self._toast_lbl = pyglet.text.Label("", x=self.width // 2, y=self.height - 74,
                                            anchor_x="center", anchor_y="center", font_size=14,
                                            color=(255, 255, 255, 0), batch=self.toast_batch)

    def on_resize(self, w, h):
        super().on_resize(w, h)
        if not hasattr(self, "l_title"):
            return
        self._p_info.y = h - 158
        self._p_help.width = w; self.l_help.width = w - 20
        self.l_title.y = h - 26; self.l_info.y = h - 52
        self._p_flash.y = h - 190; self.l_flash.y = h - 175
        cx, cy = w // 2, h // 2
        self._cross[0].x, self._cross[0].y = cx - 9, cy - 1
        self._cross[1].x, self._cross[1].y = cx - 1, cy - 9
        self._toast_bg.y = h - 96; self._toast_bg.width = w
        self._toast_lbl.x, self._toast_lbl.y = w // 2, h - 74

    # ------------------------------------------------------------ GL meshes
    def _delete_chunk_gl(self, cx, cz):
        old = self.chunks_gl.pop((cx, cz), None)
        if old:
            vao, nidx, bufs, _y0, _y1 = old
            self.tris -= nidx // 3
            glDeleteVertexArrays(1, GLuint(vao))
            for b in bufs:
                glDeleteBuffers(1, GLuint(b))

    def _upload_chunk(self, cx, cz):
        self._delete_chunk_gl(cx, cz)
        m = mesher.build_chunk_mesh(self.world, cx, cz)
        if not m:
            self.chunks_gl[(cx, cz)] = (0, 0, [], 0, 0)     # meshed-but-empty marker
            return
        pos, uv, col, idx = (np.ascontiguousarray(a, dt) for a, dt in
                             zip(m, (np.float32, np.float32, np.float32, np.uint32)))
        y0 = float(pos[:, 1].min()); y1 = float(pos[:, 1].max()) + 1.0
        vao = GLuint(); glGenVertexArrays(1, vao); glBindVertexArray(vao)
        b0 = _buffer(GL_ARRAY_BUFFER, pos)
        glEnableVertexAttribArray(0); glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, 0)
        b1 = _buffer(GL_ARRAY_BUFFER, col)
        glEnableVertexAttribArray(1); glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 0, 0)
        b2 = _buffer(GL_ARRAY_BUFFER, uv)
        glEnableVertexAttribArray(2); glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, 0, 0)
        _buffer(GL_ELEMENT_ARRAY_BUFFER, idx)
        glBindVertexArray(0)
        self.chunks_gl[(cx, cz)] = (int(vao.value), len(idx),
                                    [int(b0.value), int(b1.value), int(b2.value)], y0, y1)
        self.tris += len(idx) // 3

    def _touched(self, x, z):
        cx, lx = divmod(x, CHUNK_X); cz, lz = divmod(z, CHUNK_Z)
        s = {(cx, cz)}
        if lx == 0: s.add((cx - 1, cz))
        if lx == CHUNK_X - 1: s.add((cx + 1, cz))
        if lz == 0: s.add((cx, cz - 1))
        if lz == CHUNK_Z - 1: s.add((cx, cz + 1))
        return s

    def _restream(self):
        """Rebuild the mesh queue: every in-range chunk not yet meshed, nearest first."""
        ccx = int(math.floor(self.pos[0])) // CHUNK_X
        ccz = int(math.floor(self.pos[2])) // CHUNK_Z
        if (ccx, ccz) == (self._last_ccx, self._last_ccz):
            return
        self._last_ccx, self._last_ccz = ccx, ccz
        r = self.render_dist
        want = []
        for (cx, cz) in self.world.chunks:
            if (cx, cz) in self.chunks_gl or (cx, cz) in self.queued:
                continue
            d = max(abs(cx - ccx), abs(cz - ccz))
            if d <= r:
                want.append((d, cx, cz))
        want.sort()
        for _d, cx, cz in want:
            self.pending.append((cx, cz)); self.queued.add((cx, cz))

    def _stream(self):
        """Mesh queued chunks within a per-frame time budget."""
        if not self.pending:
            return
        t0 = time.perf_counter()
        while self.pending and time.perf_counter() - t0 < MESH_BUDGET_S:
            cx, cz = self.pending.pop(0)
            self.queued.discard((cx, cz))
            if (cx, cz) not in self.chunks_gl:
                self._upload_chunk(cx, cz)

    # ------------------------------------------------------------ editing
    def _set(self, x, y, z, blk, dirty, op=None):
        nid, nmeta = blk
        old, oldm = self.world.block(x, y, z), self.world.meta(x, y, z)
        if old == nid and oldm == nmeta:
            return
        if self.world.set_block(x, y, z, nid, nmeta) is not None:
            dirty |= self._touched(x, z)
            if op is not None:
                op.append((x, y, z, old, oldm, nid, nmeta))

    def _apply(self, dirty):
        for (cx, cz) in dirty:
            if (cx, cz) in self.world.chunks:
                self._upload_chunk(cx, cz)
        if dirty:
            self._mark_dirty()

    def _push_undo(self, op):
        if op:
            self.undo_stack.append(op)
            del self.undo_stack[:-200]           # cap history
            self.redo_stack.clear()

    def _restore(self, op, use_old):
        """Re-apply an op forward (use_old=False) or reversed (use_old=True), re-mesh."""
        dirty = set()
        for x, y, z, oid, om, nid, nm in op:
            bid, meta = (oid, om) if use_old else (nid, nm)
            if self.world.set_block(x, y, z, bid, meta) is not None:
                dirty |= self._touched(x, z)
        self._apply(dirty)

    def undo(self):
        if not self.undo_stack:
            self._flash("nothing to undo"); return
        op = self.undo_stack.pop(); self._restore(op, True)
        self.redo_stack.append(op); self._flash("undo · %d blocks" % len(op))

    def redo(self):
        if not self.redo_stack:
            self._flash("nothing to redo"); return
        op = self.redo_stack.pop(); self._restore(op, False)
        self.undo_stack.append(op); self._flash("redo · %d blocks" % len(op))

    def place_one(self, cell, blk):
        blk = blk if isinstance(blk, tuple) else (blk, 0)
        op = []; dirty = set()
        self._set(cell[0], cell[1], cell[2], blk, dirty, op); self._apply(dirty); self._push_undo(op)

    def do_box(self, a, b, blk):
        blk = blk if isinstance(blk, tuple) else (blk, 0)
        x0, x1 = sorted((a[0], b[0])); y0, y1 = sorted((a[1], b[1])); z0, z1 = sorted((a[2], b[2]))
        op = []; dirty = set(); n = 0
        for x in range(x0, x1 + 1):
            for z in range(z0, z1 + 1):
                if not self.world.has_chunk(x, z):
                    continue
                for y in range(max(0, y0), min(CHUNK_Y - 1, y1) + 1):
                    self._set(x, y, z, blk, dirty, op); n += 1
        self._apply(dirty); self._push_undo(op)
        return n

    # ------------------------------------------------------------ feature actions
    def _teleport(self, x, y, z):
        self.pos = [float(x), float(y), float(z)]
        self._last_ccx = self._last_ccz = None       # force a re-stream around the new spot

    def go_spawn(self):
        self._teleport(*self._safe_start()); self._flash("→ spawn")

    def go_player(self):
        pp = self.world.player_pos
        if pp is None:
            self._flash("no saved player position"); return
        self._teleport(pp[0], pp[1] + 2.0, pp[2]); self._flash("→ player  %d %d %d" % tuple(map(int, pp)))

    def _eyedrop(self):
        if self.aim_hit:
            self.cur = (self.world.block(*self.aim_hit), self.world.meta(*self.aim_hit))
            self._flash("picked  %s" % _blkname(self.cur))

    def _toggle_bright(self):
        self.bright_mode = (self.bright_mode + 1) % 3
        self._flash(["lighting: normal", "lighting: fullbright", "lighting: night"][self.bright_mode])

    def _change_dist(self, delta):
        self.render_dist = max(4, min(MAX_RENDER_DIST, self.render_dist + delta))
        self._dist_user_set = True                 # honour the manual choice on reload
        self._last_ccx = self._last_ccz = None
        # drop meshes now out of range so memory doesn't grow unbounded
        ccx = int(math.floor(self.pos[0])) // CHUNK_X; ccz = int(math.floor(self.pos[2])) // CHUNK_Z
        for (cx, cz) in [k for k in self.chunks_gl
                         if max(abs(k[0] - ccx), abs(k[1] - ccz)) > self.render_dist]:
            self._delete_chunk_gl(cx, cz)
        self._flash("render distance: %d chunks" % self.render_dist)

    def _sel_corner(self):
        if self.aim_hit is None:
            return
        self._wand = None                                # hand-drawn box overrides a J wand
        if self.sel_a is None or self.sel_b is not None:
            self.sel_a = self.aim_hit; self.sel_b = None; self._flash("selection corner A set")
        else:
            self.sel_b = self.aim_hit
            w, h, d = (abs(self.sel_a[i] - self.sel_b[i]) + 1 for i in range(3))
            self._flash("selection %d×%d×%d" % (w, h, d))

    def _paste_arrays(self, b, d, ox, oy, oz):
        """Overlay a schematic (b/d [W,H,L], x/y/z) at (ox,oy,oz), numpy-scattered per
        chunk so even a whole ship pastes in a moment. Records edits so it exports, and
        returns (dirty chunks, blocks placed). Air in the schematic is skipped (overlay)."""
        import math
        W, H, L = b.shape
        wch, wdt, wed = self.world.chunks, self.world.data, self.world.edits
        dirty = set(); total = 0
        for cx in range(math.floor(ox / CHUNK_X), math.floor((ox + W - 1) / CHUNK_X) + 1):
            for cz in range(math.floor(oz / CHUNK_Z), math.floor((oz + L - 1) / CHUNK_Z) + 1):
                arr = wch.get((cx, cz))
                if arr is None:
                    continue
                dat = wdt.get((cx, cz))
                wx0 = max(ox, cx * CHUNK_X); wx1 = min(ox + W - 1, cx * CHUNK_X + CHUNK_X - 1)
                wz0 = max(oz, cz * CHUNK_Z); wz1 = min(oz + L - 1, cz * CHUNK_Z + CHUNK_Z - 1)
                if wx1 < wx0 or wz1 < wz0:
                    continue
                i0, k0 = wx0 - ox, wz0 - oz
                dx, dz = wx1 - wx0 + 1, wz1 - wz0 + 1
                sub = b[i0:i0 + dx, :H, k0:k0 + dz].transpose(0, 2, 1)       # -> [dx,dz,H]
                subd = d[i0:i0 + dx, :H, k0:k0 + dz].transpose(0, 2, 1) if d is not None else None
                lx0, lz0 = wx0 - cx * CHUNK_X, wz0 - cz * CHUNK_Z
                mask = sub != 0
                if not mask.any():
                    continue
                arr[lx0:lx0 + dx, lz0:lz0 + dz, oy:oy + H][mask] = sub[mask]
                if dat is not None and subd is not None:
                    dat[lx0:lx0 + dx, lz0:lz0 + dz, oy:oy + H][mask] = subd[mask]
                for a, c, yy in np.argwhere(mask):                          # record for export
                    wed[(wx0 + int(a), oy + int(yy), wz0 + int(c))] = (
                        int(sub[a, c, yy]), int(subd[a, c, yy]) if subd is not None else 0)
                dirty.add((cx, cz)); total += int(mask.sum())
        return dirty, total

    def _stamp_into(self, b, d, ox, oy, oz, label="pasted", tes=None):
        """Overlay a schematic at (ox,oy,oz). Small builds keep per-block undo; big ones
        (>200k) take the fast numpy path (undo unavailable). `tes` (tile-entities with
        RELATIVE coords) are queued so chests/signs are written into the save on export."""
        W, H, L = b.shape
        if oy + H > CHUNK_Y:                                  # keep it under the ceiling
            oy = CHUNK_Y - H
        oy = max(0, oy)
        if H > CHUNK_Y:
            self._flash("schematic is %d tall > %d — scale it down (Tools)" % (H, CHUNK_Y)); return
        if tes:
            self.world.pasted_tes.append((tes, ox, oy, oz))
        if W * H * L <= 200000:                               # small: per-block + undo
            op = []; dirty = set(); n = 0
            for i in range(W):
                for j in range(min(H, CHUNK_Y - oy)):
                    for k in range(L):
                        bid = int(b[i, j, k])
                        if bid == 0:
                            continue
                        self._set(ox + i, oy + j, oz + k,
                                  (bid, int(d[i, j, k]) if d is not None else 0), dirty, op); n += 1
            self._apply(dirty); self._push_undo(op)
            self._flash("%s %d×%d×%d (%d blocks) — Ctrl+Z to undo" % (label, W, H, L, n))
        else:                                                 # big: fast, no undo
            dirty, n = self._paste_arrays(b, d, ox, oy, oz)
            self._apply(dirty)
            self._flash("%s %d×%d×%d (%d blocks) — large paste, undo unavailable" % (label, W, H, L, n))

    def _grab_tile_entities(self, x0, y0, z0, x1, y1, z1):
        """Read the chests/signs/etc. inside a box from the ORIGINAL save (the view3d
        World only has block ids). Coords come back relative to the box's min corner.
        Best-effort: never blocks a copy/export if the save can't be re-opened."""
        src = getattr(self.world, "source", None)
        if not src:
            return []
        try:
            from .. import World as _LW
            from .. import schematic as S
            lw = _LW.open(src)
            return S.extract_tile_entities(lw, x0, y0, z0, x1, y1, z1)
        except Exception:
            return []

    def _wand_tile_entities(self, b):
        """Tile-entities for a J structure: grab those in its bounding box, then keep only
        the ones sitting on a captured (non-air) block, so a chest on the seabed inside the
        box but off the ship doesn't come along as an orphan."""
        if self.sel_a is None or self.sel_b is None:
            return []
        tes = self._grab_tile_entities(self.sel_a[0], self.sel_a[1], self.sel_a[2],
                                       self.sel_b[0], self.sel_b[1], self.sel_b[2])
        W, H, L = b.shape
        kept = []
        for t in tes:
            rx, ry, rz = t.get_value("x"), t.get_value("y"), t.get_value("z")
            if rx is None:
                continue
            if 0 <= rx < W and 0 <= ry < H and 0 <= rz < L and b[rx, ry, rz] != 0:
                kept.append(t)
        return kept

    def copy_sel(self):
        if not (self.sel_a and self.sel_b):
            self._flash("select a box first (K sets corners, or J a structure)"); return
        from .. import schematic as S                     # numpy extract -> fast even for huge ships
        if self._wand is not None:                        # J structure: copy the EXACT ship shape
            arr, marr = self._wand
            self.clipboard = (arr, marr)
            self.clipboard_tes = self._wand_tile_entities(arr)
            tail = (" +%d chest/sign" % len(self.clipboard_tes)) if self.clipboard_tes else ""
            self._flash("copied ship: %d blocks%s (exact shape)" % (int((arr != 0).sum()), tail))
            return
        arr, marr = S.extract(self.world, self.sel_a[0], self.sel_a[1], self.sel_a[2],
                              self.sel_b[0], self.sel_b[1], self.sel_b[2])
        self.clipboard = (arr, marr)
        self.clipboard_tes = self._grab_tile_entities(self.sel_a[0], self.sel_a[1], self.sel_a[2],
                                                       self.sel_b[0], self.sel_b[1], self.sel_b[2])
        tail = (" +%d chest/sign" % len(self.clipboard_tes)) if self.clipboard_tes else ""
        self._flash("copied %d×%d×%d (%d blocks%s)" % (*arr.shape, int((arr != 0).sum()), tail))

    def paste(self):
        origin = self.aim_adj or self.aim_hit
        if origin is None:
            self._flash("aim somewhere to paste"); return
        ox, oy, oz = origin
        if self.clipboard is None:
            self._flash("clipboard empty — J a structure or Ctrl+C a K-box first"); return
        arr, marr = self.clipboard
        self._stamp_into(arr, marr, ox, oy, oz, label="pasted", tes=self.clipboard_tes)

    def _pick_file(self, save):
        """Modal file dialog (tkinter) for schematic export/import."""
        import tkinter as tk
        from tkinter import filedialog
        self.set_exclusive_mouse(False); self.captured = False
        r = tk.Tk(); r.withdraw()
        try:
            r.attributes("-topmost", True)
            ft = [("Schematic", "*.schematic"), ("All files", "*.*")]
            if save:
                return filedialog.asksaveasfilename(parent=r, defaultextension=".schematic",
                                                    initialfile="build.schematic", filetypes=ft)
            return filedialog.askopenfilename(parent=r, filetypes=ft)
        finally:
            r.destroy()

    def select_structure(self):
        """Magic wand: flood-fill the build under the crosshair and capture its EXACT
        shape. The fill crosses block FACES only (6-connectivity), so it stays on the
        connected structure instead of leaking off a hull through a diagonal gap.

        If you have a hand-drawn K box set, the flood is CLAMPED to it — the only way to
        pull a build that is physically joined to something else (a ship sitting in a dry
        dock, a house built onto a wall) out on its own: rough-box the ship with K, then J
        fills only the connected blocks inside the box and never runs out into the dock.
        Raise the box's lower edge above the dock floor to leave the floor behind.
        Ctrl+C / Ctrl+E then take just those blocks — no surrounding water, air or dock."""
        if self.aim_hit is None:
            self._flash("aim at a build, then J"); return
        from .. import schematic as S
        bounds = None
        if self.sel_a and self.sel_b and self._wand is None:      # a hand-drawn box limits the flood
            bounds = (self.sel_a[0], self.sel_a[1], self.sel_a[2],
                      self.sel_b[0], self.sel_b[1], self.sel_b[2])
        # Inside a box, corner (diagonal) connectivity is safe -- the box walls in any leak --
        # and it catches thin rigging/string glass and corner-attached funnel blocks a
        # face-only fill would drop. Free-standing (no box) stays face-only to avoid leaks.
        cells = S.select_structure(self.world, *self.aim_hit, bounds=bounds,
                                   diagonal=bounds is not None)
        if not cells:
            if bounds is not None:
                self._flash("aim at a placed block INSIDE the box (planks/wool/brick…), then J")
            else:
                self._flash("that's terrain — aim at a placed block (planks/wool/brick…)")
            return
        b, d, origin = S.cells_to_arrays(self.world, cells)      # masked to the exact shape
        self._wand = (b, d)
        self._wand_origin = origin
        self._wand_cells = cells
        x0, y0, z0 = origin
        W, H, L = b.shape
        self.sel_a = (x0, y0, z0)
        self.sel_b = (x0 + W - 1, y0 + H - 1, z0 + L - 1)
        fill = 100.0 * len(cells) / max(1, W * H * L)
        if bounds is not None:
            self._flash("ship isolated inside your box: %d blocks · %d×%d×%d — Ctrl+C copy · Ctrl+E export (exact shape)"
                        % (len(cells), W, H, L))
        else:
            self._flash("selected %d blocks · %d×%d×%d · %.0f%% filled — if this grabbed too much, draw a K box "
                        "around just the ship and press J again" % (len(cells), W, H, L, fill))

    def export_schematic(self):
        from .. import schematic as S
        import os as _os
        tes = []
        if self._wand is not None:                          # magic-wand structure (exact shape)
            b, d = self._wand
            tes = self._wand_tile_entities(b)
        elif self.sel_a and self.sel_b:                     # K-box selection
            b, d = S.extract(self.world, self.sel_a[0], self.sel_a[1], self.sel_a[2],
                             self.sel_b[0], self.sel_b[1], self.sel_b[2])
            tes = self._grab_tile_entities(self.sel_a[0], self.sel_a[1], self.sel_a[2],
                                           self.sel_b[0], self.sel_b[1], self.sel_b[2])
        else:
            self._flash("select first: J on a structure, or K on two corners"); return
        path = self._pick_file(save=True)
        if not path:
            return
        W, H, L = S.save(b, d, path, name=_os.path.splitext(_os.path.basename(path))[0],
                         tile_entities=tes)
        tail = (" +%d chest/sign" % len(tes)) if tes else ""
        self._flash("exported %d×%d×%d%s → %s" % (W, H, L, tail, _os.path.basename(path)))

    def import_schematic(self):
        origin = self.aim_adj or self.aim_hit
        if origin is None:
            self._flash("aim somewhere, then Ctrl+I to import"); return
        path = self._pick_file(save=False)
        if not path:
            return
        from .. import schematic as S
        try:
            b, d, tes = S.load(path)
        except Exception as e:
            self._flash("bad schematic: %s" % e); return
        self._stamp_into(b, d, origin[0], origin[1], origin[2], label="imported", tes=tes)

    def locate_current(self):
        bid = self.cur[0]
        if self.locate_id == bid and self.locate_cells:
            self.locate_id = None; self.locate_cells = []; self._flash("locate off"); return
        cells = []
        px, py, pz = self.pos
        rng = 96
        for (cx, cz), arr in self.world.chunks.items():
            xs, zs, ys = np.nonzero(arr == bid)
            if not len(xs):
                continue
            wx = cx * CHUNK_X + xs; wz = cz * CHUNK_Z + zs
            for a, b, c in zip(wx.tolist(), ys.tolist(), wz.tolist()):
                if abs(a - px) < rng and abs(c - pz) < rng:
                    cells.append((a, b, c))
        cells.sort(key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2 + (p[2] - pz) ** 2)
        self.locate_id = bid; self.locate_cells = cells[:400]
        self._flash("located %d× %s within %dm" % (len(cells), blocks.name(bid), rng))

    # ------------------------------------------------------------ raycast
    def _forward(self):
        cp = math.cos(self.pitch)
        return (math.cos(self.yaw) * cp, math.sin(self.pitch), math.sin(self.yaw) * cp)

    def raycast(self, maxd=72.0):
        ox, oy, oz = self.pos
        dx, dy, dz = self._forward()
        n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        dx, dy, dz = dx / n, dy / n, dz / n
        x, y, z = math.floor(ox), math.floor(oy), math.floor(oz)
        sx = 1 if dx > 0 else -1; sy = 1 if dy > 0 else -1; sz = 1 if dz > 0 else -1
        tmx = ((x + (sx > 0)) - ox) / dx if dx else math.inf
        tmy = ((y + (sy > 0)) - oy) / dy if dy else math.inf
        tmz = ((z + (sz > 0)) - oz) / dz if dz else math.inf
        tdx = abs(1.0 / dx) if dx else math.inf
        tdy = abs(1.0 / dy) if dy else math.inf
        tdz = abs(1.0 / dz) if dz else math.inf
        prev = (x, y, z)
        blk = self.world.block; has = self.world.has_chunk
        for _ in range(int(maxd * 3) + 3):
            if 0 <= y < CHUNK_Y and has(x, z) and blk(x, y, z) != 0:
                return (x, y, z), prev
            prev = (x, y, z)
            if tmx <= tmy and tmx <= tmz:
                x += sx; tmx += tdx
            elif tmy <= tmz:
                y += sy; tmy += tdy
            else:
                z += sz; tmz += tdz
            if tmx > maxd and tmy > maxd and tmz > maxd:
                break
        return None, None

    # ------------------------------------------------------------ update
    def update(self, dt):
        if self.loading:
            if self._progress.get("phase") == "ready" and "w" in self._holder:
                self._finalize_load(self._holder["w"])
            return
        dt = min(dt, 0.1)
        self._fps_n += 1
        if time.time() - self._fps_t >= 0.4:
            self._fps = self._fps_n / (time.time() - self._fps_t)
            self._fps_t = time.time(); self._fps_n = 0
        if not self.palette_open and self.typing is None:
            k, key = self.keys, pyglet.window.key
            speed = self.fly_speed * (3.3 if k[key.LSHIFT] else 1.0) * dt
            fx, fy, fz = self._forward()
            rx, rz = math.cos(self.yaw + math.pi / 2), math.sin(self.yaw + math.pi / 2)
            if k[key.W]: self.pos[0] += fx * speed; self.pos[1] += fy * speed; self.pos[2] += fz * speed
            if k[key.S]: self.pos[0] -= fx * speed; self.pos[1] -= fy * speed; self.pos[2] -= fz * speed
            if k[key.D]: self.pos[0] += rx * speed; self.pos[2] += rz * speed
            if k[key.A]: self.pos[0] -= rx * speed; self.pos[2] -= rz * speed
            if k[key.SPACE]: self.pos[1] += speed
            if k[key.LCTRL]: self.pos[1] -= speed
        self._restream()
        self._stream()
        self.aim_hit, self.aim_adj = self.raycast()
        if self.drag_btn is not None:
            b = self.aim_adj if self.drag_btn == "place" else self.aim_hit
            if b:
                self.dragB = b

    # ------------------------------------------------------------ input
    def on_mouse_press(self, x, y, button, mods):
        if self.palette_open:
            self._palette_pick(x, y); return
        if not self.captured:
            self.set_exclusive_mouse(True); self.captured = True; return
        if button == pyglet.window.mouse.LEFT:
            self.drag_btn = "place"; self.dragA = self.dragB = self.aim_adj
        elif button == pyglet.window.mouse.RIGHT:
            self.drag_btn = "remove"; self.dragA = self.dragB = self.aim_hit
        elif button == pyglet.window.mouse.MIDDLE:
            self._eyedrop()

    def on_mouse_scroll(self, x, y, sx, sy):
        if self.palette_open:
            return
        self.fly_speed = max(4.0, min(120.0, self.fly_speed * (1.15 if sy > 0 else 1 / 1.15)))
        self._flash("fly speed: %.0f" % self.fly_speed)

    def _look(self, dx, dy):
        if self.captured and not self.palette_open:
            self.yaw += dx * 0.0025
            self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.0025))

    def on_mouse_motion(self, x, y, dx, dy):
        self._look(dx, dy)

    def on_mouse_drag(self, x, y, dx, dy, buttons, mods):
        self._look(dx, dy)

    def on_mouse_release(self, x, y, button, mods):
        if self.drag_btn is None or self.dragA is None or self.dragB is None:
            self.drag_btn = None; return
        a, b, mode = self.dragA, self.dragB, self.drag_btn
        self.drag_btn = self.dragA = self.dragB = None
        if a == b:
            self.place_one(a, self.cur if mode == "place" else (0, 0)); return
        n = self.do_box(a, b, self.cur if mode == "place" else (0, 0))
        self._flash("%s %d blocks" % ("Filled" if mode == "place" else "Removed", n))

    def on_key_press(self, symbol, mods):
        key = pyglet.window.key
        # -- coordinate-entry mode swallows keys until Enter/Esc --
        if self.typing is not None:
            if symbol in (key.ENTER, key.RETURN):
                self._commit_teleport()
            elif symbol == key.ESCAPE:
                self.typing = None; self._flash("cancelled")
            elif symbol == key.BACKSPACE:
                self.typing = self.typing[:-1]
            return
        ctrl = mods & key.MOD_CTRL
        if symbol == key.ESCAPE:
            if self.palette_open:
                self.palette_open = False
            else:
                self.set_exclusive_mouse(False); self.captured = False
        elif ctrl and symbol == key.E:
            self.export_schematic()
        elif symbol == key.E:
            self.palette_open = not self.palette_open
            self.set_exclusive_mouse(not self.palette_open and self.captured)
        elif ctrl and symbol == key.I:
            self.import_schematic()
        elif ctrl and symbol == key.S:
            self._export()
        elif ctrl and symbol == key.Z:
            self.undo()
        elif ctrl and symbol == key.Y:
            self.redo()
        elif ctrl and symbol == key.C:
            self.copy_sel()
        elif ctrl and symbol == key.V:
            self.paste()
        elif symbol == key.HOME:
            self.go_spawn()
        elif symbol == key.P:
            self.go_player()
        elif symbol == key.T:
            self.typing = ""; self.set_exclusive_mouse(False); self.captured = False
        elif symbol == key.F11:
            self.set_fullscreen(not self.fullscreen)
        elif symbol == key.B:
            self._toggle_bright()
        elif symbol == key.G:
            self.show_borders = not self.show_borders
            self._flash("chunk borders %s" % ("on" if self.show_borders else "off"))
        elif symbol == key.M:
            self.show_entities = not self.show_entities
            self._flash("entity markers %s  (%d in world)"
                        % ("on" if self.show_entities else "off", len(self.world.entities)))
        elif symbol == key.K:
            self._wand = None                               # switch to manual box mode
            self._sel_corner()
        elif symbol == key.J:
            self.select_structure()
        elif symbol == key.F:
            self.locate_current()
        elif symbol == key.BRACKETLEFT:
            self._change_dist(-2)
        elif symbol == key.BRACKETRIGHT:
            self._change_dist(+2)
        elif key._1 <= symbol <= key._9:
            i = symbol - key._1
            if i < len(PALETTE):
                self.cur = PALETTE[i]; self._flash("selected %s" % _blkname(self.cur))

    def on_text(self, text):
        if self.typing is not None:
            for ch in text:
                if ch in "0123456789 -.,":
                    self.typing += (" " if ch == "," else ch)

    def _commit_teleport(self):
        parts = self.typing.replace(",", " ").split()
        self.typing = None
        try:
            x, y, z = (float(p) for p in parts[:3])
            self._teleport(x, y, z); self._flash("→ %d %d %d" % (int(x), int(y), int(z)))
        except Exception:
            self._flash("need three numbers: X Y Z")

    def _flash(self, m):
        self.flash = m; self.flash_t = time.time()

    def _update_title(self):
        self.set_caption("LCE World Viewer — %s — %d edits%s"
                         % (self.world.name, len(self.world.edits),
                            "  ● UNSAVED" if self._dirty else ""))

    def _mark_dirty(self):
        self._dirty = True; self._update_title()

    def _do_toast(self, msg, good=True):
        self.toast = msg; self.toast_t = time.time(); self.toast_good = good

    def _export(self):
        try:
            path, applied = self.world.export(backup=True)
            self._dirty = False; self._update_title()
            self._do_toast("SAVED  ✓   %d edits written · backup kept   (load it in-game)" % applied, True)
            print("exported %d edits to %s" % (applied, path), flush=True)
        except Exception as e:
            self._do_toast("SAVE FAILED:  %s" % e, False)
            print("export failed:", e, flush=True)

    # ------------------------------------------------------------ palette
    def _palette_rects(self):
        cols, size, pad = 13, 42, 6
        rows = (len(PALETTE) + cols - 1) // cols
        gw = cols * (size + pad) - pad
        gh = rows * (size + pad) - pad
        x0 = (self.width - gw) // 2
        y0 = (self.height + gh) // 2 - size
        for i, bid in enumerate(PALETTE):
            r, c = i // cols, i % cols
            yield bid, x0 + c * (size + pad), y0 - r * (size + pad), size

    def _palette_pick(self, mx, my):
        for bid, x, y, s in self._palette_rects():
            if x <= mx <= x + s and y <= my <= y + s:
                self.cur = bid; self.palette_open = False
                self.set_exclusive_mouse(self.captured)
                self._flash("selected %s" % _blkname(bid)); return

    def _build_palette_gl(self):
        """One static VAO of all swatch quads (positions + uv), rebuilt only when the
        window size changes — never per frame (the old code leaked 160 GL objects a
        frame here)."""
        pos = []; uv = []
        self._pal_hitboxes = []
        for bid, x, y, s in self._palette_rects():
            self._pal_hitboxes.append((bid, x, y, s))
            if bid[0] == 0:
                u0 = v0 = u1 = v1 = 0.0                     # eraser: sample nothing (drawn as panel)
            else:
                u0, v0, u1, v1 = _tile_uv(bid[0], bid[1])
            pos += [x, y, 0, x + s, y, 0, x + s, y + s, 0, x, y, 0, x + s, y + s, 0, x, y + s, 0]
            uv += [u0, v1, u1, v1, u1, v0, u0, v1, u1, v0, u0, v0]
        pa = np.array(pos, np.float32); ua = np.array(uv, np.float32)
        col = np.ones(len(pa), np.float32)
        vao = GLuint(); glGenVertexArrays(1, vao); glBindVertexArray(vao)
        b0 = _buffer(GL_ARRAY_BUFFER, pa); glEnableVertexAttribArray(0)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, 0)
        b1 = _buffer(GL_ARRAY_BUFFER, col); glEnableVertexAttribArray(1)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 0, 0)
        b2 = _buffer(GL_ARRAY_BUFFER, ua); glEnableVertexAttribArray(2)
        glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, 0, 0)
        glBindVertexArray(0)
        self._pal_gl = (int(vao.value), len(pa) // 3, self.width, self.height)

    # ------------------------------------------------------------ flat draw
    def _draw_flat(self, verts, rgba, mode, mvp):
        if not verts:
            return
        glUseProgram(self.flat_prog.id)
        glUniformMatrix4fv(self.fu_mvp, 1, GL_FALSE, (GLfloat * 16)(*mvp))
        glUniform4f(self.fu_rgba, *rgba)
        glBindVertexArray(self._wire_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self._wire_buf)
        arr = np.ascontiguousarray(verts, np.float32)
        glBufferData(GL_ARRAY_BUFFER, arr.nbytes, arr.ctypes.data_as(ctypes.c_void_p), GL_DYNAMIC_DRAW)
        glDrawArrays(mode, 0, len(arr) // 3)
        glBindVertexArray(0)

    @staticmethod
    def _aabb_edges(x0, y0, z0, x1, y1, z1, out=None):
        c = [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1),
             (x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)]
        E = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7)]
        v = out if out is not None else []
        for i, j in E:
            v += list(c[i]) + list(c[j])
        return v

    def _box_edges(self, a, b):
        return self._aabb_edges(min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
                                max(a[0], b[0]) + 1, max(a[1], b[1]) + 1, max(a[2], b[2]) + 1)

    def _chunk_border_verts(self):
        ccx = int(math.floor(self.pos[0])) // CHUNK_X; ccz = int(math.floor(self.pos[2])) // CHUNK_Z
        v = []
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                cx, cz = ccx + dx, ccz + dz
                self._aabb_edges(cx * CHUNK_X, 0, cz * CHUNK_Z,
                                 cx * CHUNK_X + CHUNK_X, CHUNK_Y, cz * CHUNK_Z + CHUNK_Z, v)
        return v

    def _locate_verts(self):
        v = []
        for (x, y, z) in self.locate_cells:
            self._aabb_edges(x, y, z, x + 1, y + 1, z + 1, v)
        return v

    def _entity_verts(self, want_kind):
        """Marker boxes for in-range entities of one kind: mobs get a thin tall box at
        their feet, tile-entities (chests/spawners/signs) get the block cell."""
        r = self.render_dist * CHUNK_X
        px, pz = self.pos[0], self.pos[2]
        v = []
        for kind, _name, x, y, z in self.world.entities:
            if kind != want_kind or abs(x - px) > r or abs(z - pz) > r:
                continue
            if kind == "tile":
                self._aabb_edges(x - 0.5, y - 0.5, z - 0.5, x + 0.5, y + 0.5, z + 0.5, v)
            else:
                self._aabb_edges(x - 0.35, y, z - 0.35, x + 0.35, y + 1.7, z + 0.35, v)
        return v

    # ------------------------------------------------------------ draw
    def _mvp(self):
        proj = Mat4.perspective_projection(self.width / max(1, self.height), 0.1, 1600.0, fov=70)
        return proj @ _look_at(self.pos, self._forward())

    def on_draw(self):
        if self.loading:
            self._draw_load_screen()
            return
        bright, sky = [(1.0, _SKY), (1.5, _SKY), (0.45, (0.05, 0.06, 0.12))][self.bright_mode]
        glEnable(GL_DEPTH_TEST)
        glClearColor(*sky, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        mvp = self._mvp()
        far = self.render_dist * CHUNK_X
        glUseProgram(self.mesh_prog.id)
        glUniformMatrix4fv(self.u_mvp, 1, GL_FALSE, (GLfloat * 16)(*mvp))
        glUniform3f(self.u_eye, *self.pos)
        glUniform3f(self.u_fog, *sky)
        glUniform1f(self.u_fog0, far * 0.55); glUniform1f(self.u_fog1, far)
        glUniform1f(self.u_bright, bright)
        glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, self.atlas)

        planes = _frustum_planes(mvp)
        drawn = 0
        for (cx, cz), (vao, nidx, _b, y0, y1) in self.chunks_gl.items():
            if nidx == 0:
                continue
            x0, z0 = cx * CHUNK_X, cz * CHUNK_Z
            if not _aabb_in_frustum(planes, x0, y0, z0, x0 + CHUNK_X, y1, z0 + CHUNK_Z):
                continue
            glBindVertexArray(vao)
            glDrawElements(GL_TRIANGLES, nidx, GL_UNSIGNED_INT, 0)
            drawn += 1
        glBindVertexArray(0)
        self._drawn = drawn

        # overlays (depth off so they read over terrain)
        glDisable(GL_DEPTH_TEST); glLineWidth(1.0)
        if self.show_borders:
            self._draw_flat(self._chunk_border_verts(), (0.30, 0.85, 1.0, 0.5), GL_LINES, mvp)
        if self.show_entities:
            self._draw_flat(self._entity_verts("mob"), (1.0, 0.30, 0.25, 0.95), GL_LINES, mvp)
            self._draw_flat(self._entity_verts("tile"), (0.35, 0.95, 1.0, 0.9), GL_LINES, mvp)
        if self.locate_cells:
            self._draw_flat(self._locate_verts(), (1.0, 0.25, 0.9, 0.95), GL_LINES, mvp)
        if self.sel_a and self.sel_b:
            self._draw_flat(self._box_edges(self.sel_a, self.sel_b), (1.0, 0.85, 0.2, 0.95), GL_LINES, mvp)
        glLineWidth(2.0)
        if self.drag_btn and self.dragA and self.dragB:
            rgba = (1.0, 0.4, 0.35, 1.0) if self.drag_btn == "remove" else (0.45, 1.0, 0.55, 1.0)
            self._draw_flat(self._box_edges(self.dragA, self.dragB), rgba, GL_LINES, mvp)
        elif self.aim_hit and not self.palette_open:
            self._draw_flat(self._box_edges(self.aim_hit, self.aim_hit), (0.06, 0.06, 0.06, 1.0), GL_LINES, mvp)
        glEnable(GL_DEPTH_TEST)

        if self.palette_open:
            self._draw_palette()

        self._draw_hud()

    def _draw_hud(self):
        # refresh text
        loaded = len(self.chunks_gl); total = len(self.world.chunks)
        px, py, pz = (int(round(v)) for v in self.pos)
        if self.aim_hit:
            lid = self.world.block(*self.aim_hit)
            look = "%d %s  (%d %d %d)" % (lid, blocks.name(lid), *self.aim_hit)
        else:
            look = "—"
        light = ("normal", "fullbright", "night")[self.bright_mode]
        self.l_title.text = self.world.name
        self.l_info.text = ("pos  %d, %d, %d\nhand  %s\nlook  %s\n"
                            "edits  %d      fps  %.0f\nspeed  %.0f · dist  %d · light  %s\n"
                            "chunks  %d/%d · %d drawn"
                            % (px, py, pz, _blkname(self.cur), look,
                               len(self.world.edits), self._fps, self.fly_speed, self.render_dist, light,
                               loaded, total, getattr(self, "_drawn", 0)))
        self.l_help.text = (
            "WASD+Space/Ctrl move · Shift fast · wheel speed · L place · R break · drag box · MID pick · "
            "E atlas · 1-9 hotbar · Ctrl+S save · Esc cursor · Alt+F4 quit\n"
            "Home spawn · P player · T go-to · [ ] render dist · B light · G grid · "
            "J wand · K box · Ctrl+C/V copy·paste · Ctrl+E/I schematic · Ctrl+Z/Y undo · F find · M mobs")
        # flash / coord-entry line
        if self.typing is not None:
            txt = "  go to X Y Z:  %s_" % self.typing; show = True; col = (150, 220, 255, 255)
        elif self.flash and time.time() - self.flash_t < 3.5:
            txt = "  " + self.flash; show = True; col = (255, 236, 150, 255)
        else:
            txt = ""; show = False; col = (255, 236, 150, 0)
        self._p_flash.color = (30, 34, 44, 210 if show else 0)
        if show:
            self.l_flash.text = txt
            self._p_flash.width = min(self.width - 20, 24 + int(len(txt) * 7.2))
        self.l_flash.color = col
        # cull the crosshair while the palette is open
        for c in self._cross:
            c.opacity = 0 if self.palette_open else 200

        glDisable(GL_DEPTH_TEST)
        self.ui.draw()

        # toast banner
        if self.toast and time.time() - self.toast_t < 7:
            a = int(235 * min(1.0, (7 - (time.time() - self.toast_t))))
            col = (20, 120, 55) if self.toast_good else (150, 40, 40)
            self._toast_bg.color = (*col, min(235, a))
            self._toast_lbl.text = self.toast
            self._toast_lbl.color = (255, 255, 255, min(255, a + 20))
            self.toast_batch.draw()
        glEnable(GL_DEPTH_TEST)

    def _draw_palette(self):
        ortho = Mat4.orthogonal_projection(0, self.width, 0, self.height, -1, 1)
        glDisable(GL_DEPTH_TEST)
        # dim the world
        self._draw_flat([0, 0, 0, self.width, 0, 0, self.width, self.height, 0,
                         0, 0, 0, self.width, self.height, 0, 0, self.height, 0],
                        (0.0, 0.0, 0.0, 0.62), GL_TRIANGLES, ortho)
        if not self._pal_gl or self._pal_gl[2] != self.width or self._pal_gl[3] != self.height:
            self._build_palette_gl()
        # eraser + selection backing panels (flat)
        for bid, x, y, s in self._pal_hitboxes:
            if bid[0] == 0:
                self._draw_flat([x, y, 0, x + s, y, 0, x + s, y + s, 0, x, y, 0, x + s, y + s, 0, x, y + s, 0],
                                (0.20, 0.22, 0.28, 1.0), GL_TRIANGLES, ortho)
            if bid == self.cur:
                self._draw_flat([x - 3, y - 3, 0, x + s + 3, y - 3, 0, x + s + 3, y + s + 3, 0,
                                 x - 3, y - 3, 0, x + s + 3, y + s + 3, 0, x - 3, y + s + 3, 0],
                                (1.0, 0.82, 0.25, 1.0), GL_TRIANGLES, ortho)
        # all swatches in one draw call
        vao, nverts, _w, _h = self._pal_gl
        glUseProgram(self.mesh_prog.id)
        glUniformMatrix4fv(self.u_mvp, 1, GL_FALSE, (GLfloat * 16)(*ortho))
        glUniform3f(self.u_eye, 0, 0, 0)
        glUniform3f(self.u_fog, *_SKY); glUniform1f(self.u_fog0, 1e9); glUniform1f(self.u_fog1, 1e9 + 1)
        glBindTexture(GL_TEXTURE_2D, self.atlas)
        glBindVertexArray(vao)
        glDrawArrays(GL_TRIANGLES, 0, nverts)
        glBindVertexArray(0)
        glEnable(GL_DEPTH_TEST)


def run(savefile, shot=None, radius=None):
    if shot:                                       # offscreen: synchronous, one frame
        print("loading world ...", flush=True)
        t = time.time()
        world = World.load(savefile)
        print("loaded %d chunks in %.1fs" % (len(world.chunks), time.time() - t), flush=True)
        win = Editor(world, savefile=savefile, radius=radius, visible=False)
        win.render_dist = radius or 40
        win._restream()
        while win.pending:
            win._stream()
        win.switch_to(); win.dispatch_events()
        win.on_draw()
        pyglet.image.get_buffer_manager().get_color_buffer().save(shot)
        print("saved", shot, "(%d chunk meshes)" % len(win.chunks_gl), flush=True)
        win.close(); return

    # interactive: show the window instantly and load on a background thread so the
    # loading screen animates instead of a ~20s blank freeze.
    import threading
    progress, holder = {"phase": "opening save", "done": 0, "total": 1}, {}

    def _bg():
        try:
            holder["w"] = World.load(savefile, progress=progress)
            progress["phase"] = "ready"           # signals the main thread to switch in
        except Exception as e:
            progress["phase"] = "error: %s" % e
            print("load failed:", e, flush=True)

    threading.Thread(target=_bg, daemon=True).start()
    win = Editor(None, savefile=savefile, radius=radius, progress=progress, holder=holder)
    pyglet.clock.schedule_interval(win.update, 1 / 60.0)
    pyglet.app.run()


if __name__ == "__main__":
    from .__main__ import main
    sys.exit(main(sys.argv[1:]))
