import os
from pathlib import Path
import subprocess
import sys


if Path("/etc/raspiwifi/networkmanager.json").is_file():
    if os.getuid():
        sys.exit("Run the RaspiWiFi reset as root.")
    if subprocess.run(["systemctl", "is-active", "--quiet", "raspiwifi.service"], check=False).returncode:
        sys.exit("Start raspiwifi.service before requesting the setup hotspot.")
    Path("/etc/raspiwifi/start-hotspot").touch(mode=0o600)
    print("Setup hotspot requested. A Wi-Fi SSH session may disconnect.")
else:
    sys.exit("No NetworkManager installation found.")
