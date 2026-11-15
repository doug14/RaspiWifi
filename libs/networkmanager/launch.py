from pathlib import Path
import runpy
import sys


# Invoked by systemd with -I -S. Only load project code and Debian packages.
root = Path(__file__).resolve().parent
sys.path[:0] = [str(root), "/usr/lib/python3/dist-packages"]
program = sys.argv.pop(1)
runpy.run_path(str(root / program), run_name="__main__")
