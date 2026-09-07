#!/usr/bin/env python3
"""LCEStudio -- launcher / single entry point.

    LCEStudio.exe                       open the desktop editor
    LCEStudio.exe "<save-path>"         open the editor on a save
    LCEStudio.exe --view3d "<save>"     open the 3D fly-through window
                                        (used internally by the GUI's 3D button)
Works both as a script and as a frozen PyInstaller executable.
"""
import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--view3d":
        from lce.view3d.__main__ import main as view_main
        return view_main(argv[1:])
    if argv and argv[0] == "--selftest-converter":
        # verify the vendored converter's bundled DLLs + templates resolve when frozen;
        # the windowed exe has no console, so write the outcome to a REPORT file.
        # SAFETY: argv[1] is the report path, and this OVERWRITES it — so refuse any
        # target that isn't a .txt (and never one that already holds real data). This
        # prevents pointing the self-test at a save and clobbering it.
        out = argv[1] if len(argv) > 1 else "converter_selftest.txt"
        if not str(out).lower().endswith(".txt"):
            sys.stderr.write("--selftest-converter writes a report; its argument must be a .txt "
                             "path, not %r (refusing to overwrite it).\n" % out)
            return 2
        if os.path.exists(out) and os.path.getsize(out) > 1_000_000:
            sys.stderr.write("--selftest-converter refuses to overwrite the large existing file "
                             "%r.\n" % out)
            return 2
        lines = []
        try:
            from lce.converter import lce_engine as E
            from lce.converter import lce_recode as R
            from lce.converter import converter as C
            d = b"x\x00\x01" * 5000
            lines.append("xmem round-trip: %s" % (E.xmem_decompress(E.xmem_compress(d), len(d)) == d))
            lines.append("id_tables: block1=%s item264=%s" % (E.block_name(1, 0), E.item_name(264, 0)))
            lines.append("chm_lzx.dll: %s" % (C._get_chm_lzx() is not None))
            lines.append("LZXDecompression.dll: %s" % (C._get_lzx_dll() is not None))
            lines.append("recode subs: %s" % bool(R.substitutions_for(0)))
            lines.append("res base: %s" % E._HERE)
            lines.append("RESULT: PASS")
        except Exception as e:
            import traceback
            lines.append("RESULT: FAIL %r" % e)
            lines.append(traceback.format_exc())
        open(out, "w", encoding="utf-8").write("\n".join(lines))
        return 0
    from lce.gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
