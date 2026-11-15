from getpass import getpass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from secrets import token_urlsafe

from backend import (HOTSPOT_ADDRESS, PROFILE, SECURITY, UPDATE_TO_DISK, WIFI,
                     NetworkError, NetworkManager, validate_wifi)
from config import (CONFIG_DIR, CONFIG_PATH, INSTALL_DIR, LANDING_UNIT, LANDING_UNIT_PATH,
                    PENDING_SWITCH, RESET_REQUEST, SERVICE, UNIT_PATH, WEB_UNIT,
                    WEB_UNIT_PATH, load_config, save_config)
from credentials import ADMIN_CODE, PORTAL_CA, PORTAL_CERT, PORTAL_KEY, provision
from firewall import HotspotFirewall
from mdns import PORTAL_HOSTNAME


DEFAULT_HOTSPOT_SSID = "RaspWifi"


def copy_runtime(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Staging may use umask 077. The web account needs to read deployed code and assets.
    target.chmod(0o755)
    for path in target.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def yes(prompt):
    return input(prompt).strip().lower() == "y"


def install():
    if any(path.exists() for path in (CONFIG_DIR, INSTALL_DIR, UNIT_PATH, WEB_UNIT_PATH, LANDING_UNIT_PATH,
                                      Path("/etc/cron.raspiwifi"))):
        raise ValueError("A RaspiWiFi installation already exists. Uninstall it before installing this backend.")

    interface = input("Wi-Fi interface [wlan0]: ").strip() or "wlan0"
    ssid = input(f"Setup hotspot SSID [{DEFAULT_HOTSPOT_SSID}]: ") or DEFAULT_HOTSPOT_SSID
    entered_password = getpass("Hotspot WPA2 password (Enter to generate a unique password): ")
    password = entered_password or token_urlsafe(18)
    validate_wifi(ssid, password)
    auto_config = input("Enable hotspot fallback when Wi-Fi is unavailable? [Y/n]: ").strip().lower() in ("", "y")
    delay = int(input("Fallback delay in seconds [300]: ") or "300") if auto_config else 300
    karaoke = yes("Allow local PiKaraoke access on TCP 5555 from the hotspot? [y/N]: ")
    if delay <= 0:
        raise ValueError("The fallback delay must be positive.")

    print(f"The setup portal uses {PORTAL_HOSTNAME} and {HOTSPOT_ADDRESS}.")
    if not yes("Install RaspiWiFi with NetworkManager? [y/N]: "):
        return

    prepare_runtime()

    config = {"interface": interface, "owner": str(uuid.uuid4()), "ssid": ssid,
              "auto_config": auto_config, "delay": delay, "karaoke": karaoke}
    network = NetworkManager(interface, config["owner"])
    network.require_ap()
    HotspotFirewall(interface, karaoke)
    source = Path(__file__).resolve().parents[1]
    # Keep the ownership record before creating profiles so the uninstaller can recover a partial installation.
    copy_runtime(source, INSTALL_DIR)
    save_config(config)
    try:
        provision()
        network.add_hotspot(ssid, password)
        install_units(source / "networkmanager")
    except (NetworkError, OSError, subprocess.CalledProcessError):
        print(f"Installation interrupted. To clean up, run sudo python3 {INSTALL_DIR / 'uninstall.py'}.")
        raise
    print(f"Installed. Read the administrative code at {ADMIN_CODE} as root.")
    print(f"Transfer {PORTAL_CA} over a trusted channel before opening the portal.")
    print("Use nmcli --show-secrets to read the generated password from the RaspiWiFi hotspot profile.")
    print(f"To open the setup hotspot, run sudo python3 {INSTALL_DIR / 'reset_device/manual_reset.py'}.")


def prepare_runtime():
    import pwd

    packages = ["python3-gi", "python3-flask", "dnsmasq-base", "avahi-daemon", "nftables",
                "python3-cryptography", "python3-gunicorn"]
    missing = []
    for package in packages:
        result = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                                capture_output=True, text=True, check=False)
        if result.stdout != "installed":
            missing.append(package)
    if missing:
        subprocess.run(["apt-get", "update"], check=True)
        subprocess.run(["apt-get", "install", "-y", *missing], check=True)

    try:
        account = pwd.getpwnam("raspiwifi-web")
    except KeyError:
        subprocess.run(["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home",
                        "--shell", "/usr/sbin/nologin", "raspiwifi-web"], check=True)
    else:
        if account.pw_uid == 0 or account.pw_shell != "/usr/sbin/nologin":
            raise ValueError("The raspiwifi-web account is not a dedicated service account.")


def install_units(source):
    shutil.copyfile(source / SERVICE, UNIT_PATH)
    shutil.copyfile(source / WEB_UNIT, WEB_UNIT_PATH)
    shutil.copyfile(source / LANDING_UNIT, LANDING_UNIT_PATH)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)


def upgrade():
    config = load_config()
    network = NetworkManager(config["interface"], config["owner"])
    profiles = network.owned_profiles("hotspot")
    if len(profiles) != 1 or RESET_REQUEST.exists() or PENDING_SWITCH.exists():
        raise ValueError("Finish any pending network change and verify the RaspiWiFi hotspot profile first.")
    path, previous_profile = profiles[0]
    if network.active()[0].get("Connection") == path:
        raise ValueError("Connect the Pi to its normal Wi-Fi before upgrading the hotspot profile.")
    profile_uuid = previous_profile["connection"]["uuid"]
    old_password = subprocess.check_output(["nmcli", "--show-secrets", "-g", f"{SECURITY}.psk",
                                            "connection", "show", "uuid", profile_uuid], text=True).strip()
    certificates_exist = ADMIN_CODE.exists()
    password = old_password if old_password and certificates_exist else token_urlsafe(18)
    settings = network.settings(config["ssid"], password, "hotspot")
    settings["connection"]["uuid"] = network.glib.Variant("s", profile_uuid)
    original = network.settings(bytes(previous_profile[WIFI]["ssid"]).decode(), old_password, "hotspot")
    if "ap-isolation" not in previous_profile[WIFI]:
        original[WIFI].pop("ap-isolation")
    original["connection"]["uuid"] = network.glib.Variant("s", profile_uuid)
    original["connection"]["id"] = network.glib.Variant("s", previous_profile["connection"]["id"])
    if not yes("Upgrade the RaspiWiFi service and hotspot profile? [y/N]: "):
        return
    prepare_runtime()
    provision()
    source = Path(__file__).resolve().parents[1]
    unit_backups = {file: file.read_bytes() if file.exists() else None
                    for file in (UNIT_PATH, WEB_UNIT_PATH, LANDING_UNIT_PATH)}
    with tempfile.TemporaryDirectory(dir=INSTALL_DIR.parent, prefix=".raspiwifi-upgrade-") as tmp:
        temp = Path(tmp)
        staged, backup = temp / "new", temp / "previous"
        copy_runtime(source, staged)
        subprocess.run(["systemctl", "stop", SERVICE], check=True)
        try:
            os.rename(INSTALL_DIR, backup)
            os.rename(staged, INSTALL_DIR)
            network.call(path, PROFILE, "Update2", "(a{sa{sv}}ua{sv})", (settings, UPDATE_TO_DISK, {}))
            install_units(INSTALL_DIR / "networkmanager")
            subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=True)
        except (NetworkError, OSError, subprocess.CalledProcessError):
            if backup.exists():
                if INSTALL_DIR.exists():
                    os.rename(INSTALL_DIR, staged)
                os.rename(backup, INSTALL_DIR)
            network.call(path, PROFILE, "Update2", "(a{sa{sv}}ua{sv})", (original, UPDATE_TO_DISK, {}))
            for file, data in unit_backups.items():
                if data is None:
                    file.unlink(missing_ok=True)
                else:
                    file.write_bytes(data)
            if not certificates_exist:
                for file in (ADMIN_CODE, PORTAL_CA, PORTAL_CERT, PORTAL_KEY):
                    file.unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "start", SERVICE], check=True)
            raise
    print("Upgraded RaspiWiFi. Use nmcli --show-secrets to read the hotspot password.")


def uninstall():
    from config import load_config

    if not yes("Remove RaspiWiFi and the NetworkManager profiles it created? [y/N]: "):
        return
    config = load_config()
    network = NetworkManager(config["interface"], config["owner"])
    if UNIT_PATH.exists():
        subprocess.run(["systemctl", "disable", "--now", SERVICE], check=True)
    for path, _ in network.owned_profiles():
        network.delete(path)
    HotspotFirewall(config["interface"]).update(False)
    UNIT_PATH.unlink(missing_ok=True)
    WEB_UNIT_PATH.unlink(missing_ok=True)
    LANDING_UNIT_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    for path in (ADMIN_CODE, PORTAL_CA, PORTAL_CERT, PORTAL_KEY):
        path.unlink(missing_ok=True)
    RESET_REQUEST.unlink(missing_ok=True)
    PENDING_SWITCH.unlink(missing_ok=True)
    CONFIG_PATH.unlink()
    shutil.rmtree(INSTALL_DIR)
    if not any(CONFIG_DIR.iterdir()):
        CONFIG_DIR.rmdir()
    print("RaspiWiFi removed.")

