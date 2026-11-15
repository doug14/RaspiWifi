import os
from pathlib import Path
import shutil
import subprocess
import sys

if os.getuid():
    sys.exit("You need root access to install!")
if not shutil.which("nmcli") or subprocess.run(
    ["systemctl", "is-active", "--quiet", "NetworkManager"], check=False
).returncode:
    sys.exit("Install and start NetworkManager before installing RaspiWiFi.")

sys.path[:0] = [str(Path(__file__).resolve().parent / "libs" / "networkmanager"),
                "/usr/lib/python3/dist-packages"]
from backend import NetworkError
from install import install, upgrade

try:
    if sys.argv[1:] == ["--upgrade"]:
        upgrade()
    elif not sys.argv[1:]:
        install()
    else:
        sys.exit("Usage: initial_setup.py [--upgrade]")
except (NetworkError, ValueError, OSError, subprocess.CalledProcessError) as error:
    sys.exit(str(error))
