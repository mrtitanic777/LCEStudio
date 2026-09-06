# Minecraft Xbox 360 (LCE / TU0) Modding & Patching Guide

Hard-won reference for injecting content and patching the game under **Nexia360**
(a Xenia-based emulator). Title `584111F7`, retail launch build (TU0 = Beta 1.6.6),
`default.xex` filetime Mar 2012. **Read the "Golden Rules" first — they cost us hours.**

---

## 0. GOLDEN RULES (the expensive lessons)

1. **NEVER swap `default.xex` to apply a code patch.** Nexia360 identifies the game
   and applies its own patch layer by **hashing the xex** (`hash = "5FA6BE391D4387F1"`
   in the patch toml). Any modified exe → different hash → Nexia360 applies **none**
   of its patches (Resolution, Debug Menu, mousehook). The game then boots *raw* and
   freezes for reasons unrelated to your patch. **Tell-tale: `[Patches Applied]`
   disappears from the title bar.** A correct code patch can look "broken" this way
   while actually being fine.

2. **Patch through the toml instead** (see §3). Non-destructive, layered on top of
   Nexia360's patches, toggleable, and keeps the hash valid.

3. **Chunk re-compression must be console-exact.** Our own hand-rolled LZX encoder
   fools our own decoder but the console's `XMemDecompress` mis-decodes it and
   crashes on any chunk containing entity/tile bytes. Use **XexTool-RE's** verified
   LZX encoder (`lzxc.exe`). See §2.

4. **The emulator caches the world/session in memory.** Fully close Nexia360
   (Task Manager, not just exit-to-menu) before testing a changed save or patch.

5. **LCE finite worlds load ALL entities at boot**, not lazily — so you can't hide a
   crashy entity by placing it far away. Fix the entity/game, not the location.

---

## 1. Save format (retail `savegame.dat`)

```
[u32 datalen][u32 0][u32 dec_size][ XMemCompress-LZX frames @12 ]  then 0x00 pad -> 512B
```
Decompresses to a VFS blob: `[u32 index_off][u32 count][u32 ver=2]` + contiguous
files + a **144-byte** VFS index (128B UTF-16BE name + u32 len + u32 off + 8B
per-file timestamp — NOT a checksum, just preserve it).

Region files `r.X.Z.mcr`: 4096B location table + 4096B timestamps + sectors.
Chunk sector = `[u32 0x80000000|lzx_len][u32 decompLength][ LZX ]`; the top bit means
"RLE-wrapped": `DecompressLZXRLE` = XMemDecompress -> RLE-expand. RLE spec (from
`compression.cpp`): byte 255,k(<3)=k+1 x 0xFF; 255,n(>=3),b=n+1 x b; else literal.
TU0 chunks are plain legacy NBT (`0x0a`, tag `Entities`), not v8/v9 storage.
level.dat `version = 19132` (McRegion). No `MapFeatures` tag on native saves.

Tools: `lce_savegame.py` (codec), `lce_recover.py` (dual-format decode), `lzxc.exe`.

---

## 2. Console-exact chunk LZX (the writer fix)

Build the encoder from XexTool-RE (`C:\Users\mrtit\Documents\GitHub\XexTool-RE`),
g++ at `C:\msys64\mingw64\bin`:
```
g++ -std=c++17 -O2 -I<RE>/src lzxc.cpp <RE>/src/compress/lzx.cpp <RE>/src/compress/lzx_encode.cpp -o lzxc.exe
```
Compress a chunk's RLE: `lzxc in out 0x20000` -> output is `[u16 comp][bitstream]`.
Strip the 2-byte header, wrap as
`[0xFF][u16 raw=len(rle)][u16 comp=len(bitstream)][bitstream][00 00 00 00 00]`.

**CRITICAL — the 5-byte zero trailer is MANDATORY.** The console segment is
`marker+rle+comp headers (5B) + bitstream + 5 zero bytes`, and the sector's segment-length
field (`0x80000000 | seglen`) **includes** the trailer, so `seglen = comp + 10` always.
`XMemDecompress` consumes the stream by `seglen`; if the 5 trailer bytes are missing its
bit-reader runs past the end, mis-decodes, and the world **freezes at "Loading spawn area."**
Omitting the trailer cost an entire debugging session where every injected world froze
(giant, zombie, inventory, any size — all the same freeze) until the diff to the console
bytes revealed the missing 5 bytes. With the trailer, `encode_chunk` reproduces the console
sector **byte-identical on 282/288 chunks** (the other 6 are multi-segment specials; a fresh
single-segment re-encode of them is still valid). Window `0x20000`; single-frame (rle <= 0x8000).

---

## 3. Runtime code patches (the RIGHT way)

Edit `C:\Nexia360\patches\584111F7 - Minecraft (XBLA, TU0).patch.toml`. Add:
```toml
[[patch]]
    name = "My patch"
    is_enabled = true
    [[patch.be32]]        # be16 / be32 supported; value written at `address` in loaded memory
        address = 0x8255d674
        value = 0xc0099848
```
Addresses are virtual (image loads at `0x82000000`). Restart the emulator fully.
Existing enabled patches worth knowing: **Resolution**, **Debug Menu** (Credits ->
DebugMenu, How-To-Play -> Debug Overlay — do NOT open on the main menu).

RE toolchain: XexTool `-b out.bin default.xex` dumps the decrypted basefile;
disassemble with capstone (`CS_ARCH_PPC`, 32-bit big-endian). `re_helper.py` resolves
MSVC RTTI `.?AVClass@@` -> vtable and finds constructors via xref to the vtable addr.

---

## 4. Entity injection (`inject_entity.py`)

Registered TU0 mobs (from `EntityIO.cpp`): Creeper, Skeleton, Spider, **Giant(53)**,
Zombie, Slime, Ghast, PigZombie, Pig, Sheep, Cow, Chicken, Squid, Wolf, + base
Mob(48)/Monster(49). (No Enderman/Blaze/Villager — those are post-TU0.)

Inserts a mob compound into a chunk's `Entities` list, re-encodes that one chunk with
`lzxc.exe`, keeps every other chunk's original console LZX, rebuilds the retail
container. Required NBT (Entity::load derefs without null-check): `Pos`/`Motion`/
`Rotation` + `FallDistance`/`Fire`/`Air`/`OnGround`; Mob adds `Health`(default max)/
`HurtTime`/`DeathTime`/`AttackTime`. Pos header is 11 bytes; doubles at +11 (off-by-one
= NaN pos = hang). New world = new `<Name>.bin` dir with `savegame.dat` + a copied
`__thumbnail.png`; emulator scans dirs, no saveinfo entry needed.

```
python inject_entity.py <src .bin|savegame.dat> <out.dat> --mob Giant --at X Z [--count N --spread S]
```

---

## 5. The Giant — recovery + stability patch (case study)

The Giant is fully registered but has **no natural spawn**. Injecting it loads it, but
it crashes/freezes non-deterministically. RE finding: the Giant's vtable is **identical
to the Zombie's except slot 0 (destructor)** — it is a Zombie whose constructor
(`0x8255D58C`) does `health*10`, `heightOffset*=6.0`, `setSize(w*6.0,h*6.0)`,
`attackDamage=50`. The **6x hitbox** overflows a fixed collision scratch buffer and
corrupts memory (crash = jump to trampled global `0x829F04A8` = `0x01010101`).

**Fix = shrink the size multiplier.** The `6.0` at `0x82009820` is shared (25 refs), so
patch the Giant ctor's load instead: `lfs f0,-0x67e0(r9)` at **`0x8255D674`**
(`c0 09 98 20`). Repoint the displacement to a smaller float in the pool:

| Size | Float addr   | Patched instruction (be32 @0x8255d674) |
|------|--------------|----------------------------------------|
| 1.5x | 0x82009828   | `0xc0099828` |
| 2.0x | 0x82009848   | `0xc0099848`  ← confirmed stable |
| 3.75x| 0x8200980C   | `0xc009980c` |
| 6.0x | 0x82009820   | `0xc0099820`  (original, unstable) |

`f0` drives both heightOffset and setSize, so this scales the whole giant. Deliver it
as a toml patch (§3). **2x confirmed stable at spawn.** To resize, change `value` and
restart — no re-injection.

---

## 6. Cut / notable content found in the binary

- Full Java **splash-text list** baked into the C++ port (`Uses LWJGL!`, Notch splashes,
  forum shout-outs) — vestige of the Java original.
- Cut mob: **Giant** (recovered, §5). Base **Mob/Monster** classes exist too.
- Block `aprilFoolsJoke_Id = 95` = the **Locked Chest** (unobtainable).
- 4J additions: trial/DLC upsell scenes, Facebook `SocialPost`, `CScene_Debug`.

---

## 6b. Spawning the Giant from the Debug Overlay (on-demand, no save edit)

The **Debug Overlay** (the "How To Play" slot, per the Debug Menu patch) has a working
**mob picker** — clicking an entry spawns that mob at the player. It does *not* list the
Giant, and it can't be made to via a static patch, because the picker is built from a
**heap** registry (`*(0x829BBCD8)`, a 32000-slot array indexed by entity id, allocated +
zero-filled at `0x82949308`, then filled for ids 0-255 whose entry in the entity registry
is non-null — the Giant's id-53 slot is left empty).

But the spawn itself is trivial to hijack. Mechanism (RE):
- **Builder** `0x82296BA8` iterates the registry; for each non-null slot it pushes the
  **slot index (= entity id)** into `this->[0x30]` and shows the descriptor's `getName()`.
  So the display label comes from the descriptor, but the **spawn id is just the slot index**.
- **Selection** `0x82297160` (NOTIFY, msg 0xe): for the mob list (`this->[8]`) it loads the
  picked id `lwzx r5,r9,r10` @ **0x822971CC** and writes it to the **per-player pending-spawn
  globals**: value `*(0x82A1607C + player*4) = id`, flag `*(0x82A1606C + player*4) = 1` (mob;
  3 = block, 5 = other list).
- **Processor** (in the game/mode tick, ~`0x8225B5E0`+) reads the flag per player and spawns
  that id at the player through the normal create-by-id path (so the **Stable Giant ctor
  patch still applies**).

**Patch (delivered, enabled):** overwrite the picked-id load with `li r5,53`:
```toml
[[patch]]
    name = "Debug Overlay: mob list spawns Giant"
    is_enabled = true
    [[patch.be32]]
        address = 0x822971cc
        value = 0x38a00035   # li r5,53  (was lwzx r5,r9,r10 = 0x7ca9502e)
```
Now **any** entry you click in the overlay's mob list spawns a Giant at your feet. Only the
mob list is affected (block list + other pickers untouched). Requires the **Stable Giant
(2x hitbox)** patch (§5) so the spawn doesn't crash. Disable to restore normal mob spawning.
Selective remap (keep other mobs, add Giant) isn't possible in-place — it needs a compare +
branch, which won't fit; the all-Giant list is the clean single-instruction option.

## 6c. Giant mob-spawner den + spawner item (`inject_spawner.py`)

A save-edited **MobSpawner tile entity** spawns the Giant on a timer — no menu, no
code patch. Verified NBT (matches `MobSpawnerTileEntity` in `default.xex`; the tag
names live in the exe as **UTF-16BE** `EntityId`/`Delay`/id `MobSpawner`, but on
disk NBT tag names are ASCII like `Blocks`/`Entities`):
```
TAG_Compound (element of the chunk's TileEntities list)
  id       = "MobSpawner"   (String)
  x, y, z  = block coords    (Int)
  EntityId = "Giant"         (String)   <- any registered entity name; Giant works
  Delay    = 20              (Short)
```
Put it on a **block id 52** (spawner) at the same x/y/z. Monster spawners only fire
at **low light** (like a dungeon), so `inject_spawner.py` seals it in a 5x5x5 dark
stone room (roof blocks skylight → dark → it actually spawns), seeds two stable
Giants, and marks the surface above with **glowstone (id 89)**. It also drops N
**Monster Spawner blocks (id 52)** into the player's inventory.

Player data is a **separate VFS file** `players/<uid>.dat` (NOT level.dat) — a root
compound with `Inventory` (TAG_List of `{id:short, Damage:short, Count:byte,
Slot:byte}`). `build_retail` recomputes file lengths, so just mutate `filedata`.

```
python inject_spawner.py <src world> <out world.bin> --at X Y Z --items 8
```
Place the den a few blocks off the world spawn/respawn point (don't bury the spawn
column) but within 16 blocks of where the player loads, or the spawner won't tick.
Needs the **Stable Giant (2x hitbox)** patch enabled.

## 7. Revert / safety

- Undo a code patch: set `is_enabled = false` in the toml (or delete the `[[patch]]`).
- `default.xex.ORIGINAL` in the game folder is a backup of the untouched exe.
- Original giant-free world: `Save2026 817 55555.bin` (the source 289 world).
```
