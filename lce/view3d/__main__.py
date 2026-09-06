"""3D fly-through viewer:  python -m lce.view3d "<save>" [--radius N] [--shot out.png]"""
import sys

from .viewer import run


def main(argv):
    args = [a for a in argv if a]
    shot = radius = None
    if "--shot" in args:
        i = args.index("--shot"); shot = args[i + 1]; args = args[:i] + args[i + 2:]
    if "--radius" in args:
        i = args.index("--radius"); radius = int(args[i + 1]); args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__); return 1
    run(args[0], shot=shot, radius=radius)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
