import json
import os
from pathlib import Path
import tempfile


CONFIG_DIR = Path("/etc/raspiwifi")
CONFIG_PATH = CONFIG_DIR / "networkmanager.json"
RESET_REQUEST = CONFIG_DIR / "start-hotspot"
PENDING_SWITCH = CONFIG_DIR / "pending-switch.json"
INSTALL_DIR = Path("/usr/lib/raspiwifi")
SERVICE = "raspiwifi.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE
WEB_UNIT = "raspiwifi-web@.service"
WEB_UNIT_PATH = UNIT_PATH.with_name(WEB_UNIT)
LANDING_UNIT = "raspiwifi-landing.service"
LANDING_UNIT_PATH = UNIT_PATH.with_name(LANDING_UNIT)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config):
    write_json(CONFIG_PATH, config)


def write_json(path, value):
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_DIR,
                                         delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

