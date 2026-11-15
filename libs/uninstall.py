import os
from pathlib import Path
import subprocess
import sys

if os.getuid():
    sys.exit("Run the RaspiWiFi uninstaller as root.")
if not Path("/etc/raspiwifi/networkmanager.json").is_file():
    sys.exit("No NetworkManager installation found. Use the original release to remove a legacy installation.")

sys.path[:0] = [str(Path(__file__).resolve().parent / "networkmanager"), "/usr/lib/python3/dist-packages"]
from backend import NetworkError
from install import uninstall

try:
    uninstall()
except (NetworkError, OSError, subprocess.CalledProcessError) as error:
    sys.exit(str(error))
