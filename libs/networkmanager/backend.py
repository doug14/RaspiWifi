import ipaddress
import json
import logging
import re
import time
import uuid

from config import PENDING_SWITCH, write_json


NM = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"
SETTINGS = NM + ".Settings"
PROFILE = SETTINGS + ".Connection"
DEVICE = NM + ".Device"
WIRELESS = DEVICE + ".Wireless"
ACTIVE = NM + ".Connection.Active"
WIFI = "802-11-wireless"
SECURITY = WIFI + "-security"
OWNER_KEY = "org.raspiwifi.owner"
ROLE_KEY = "org.raspiwifi.role"
HOTSPOT_ADDRESS = "10.0.0.1"
ACTIVATION_TIMEOUT = 45
AP_CAPABILITY = 0x40
STATE_ACTIVATED = 2
DEVICE_STATE_FAILED = 120
UPDATE_TO_DISK = 1


class NetworkError(RuntimeError):
    pass


class WiFiValidationError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def validate_wifi(ssid, password):
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(c in ssid for c in "\0\r\n"):
        raise WiFiValidationError("ssid", "Enter an SSID between 1 and 32 bytes.")
    if password and not (re.fullmatch(r"[ -~]{8,63}", password) or
                         re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise WiFiValidationError("password", "Enter a WPA password of 8–63 printable ASCII characters or a 64-digit hex key.")


class NetworkManager:
    def __init__(self, interface, owner):
        from gi.repository import Gio, GLib

        self.gio = Gio
        self.glib = GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.interface = interface
        self.owner = owner

    def call(self, path, interface, method, signature=None, values=()):
        params = self.glib.Variant(signature, values) if signature else None
        try:
            return self.bus.call_sync(NM, path, interface, method, params, None,
                                      self.gio.DBusCallFlags.NONE, 5000, None).unpack()
        except self.glib.Error as error:
            name = self.gio.DBusError.get_remote_error(error)
            # The service must never log a D-Bus error message containing Wi-Fi credentials.
            raise NetworkError(f"{method}: {name or 'D-Bus request failed'}") from None

    def properties(self, path, interface):
        return self.call(path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", (interface,))[0]

    def device(self):
        return self.call(ROOT, NM, "GetDeviceByIpIface", "(s)", (self.interface,))[0]

    def require_ap(self):
        device = self.device()
        if not self.properties(device, DEVICE).get("Managed"):
            raise NetworkError(f"{self.interface} is not managed by NetworkManager.")
        if not self.properties(device, WIRELESS).get("WirelessCapabilities", 0) & AP_CAPABILITY:
            raise NetworkError(f"{self.interface} does not advertise access-point support.")

    def profile_settings(self, path):
        return self.call(path, PROFILE, "GetSettings")[0]

    def owned_profiles(self, role=None):
        profiles = []
        for path in self.call(ROOT + "/Settings", SETTINGS, "ListConnections")[0]:
            try:
                settings = self.profile_settings(path)
            except NetworkError as error:
                if not str(error).endswith((".UnknownObject", ".UnknownMethod")):
                    raise
                continue
            data = settings.get("user", {}).get("data", {})
            if data.get(OWNER_KEY) == self.owner and (role is None or data.get(ROLE_KEY) == role):
                profiles.append((path, settings))
        return profiles

    def active(self, device=None):
        path = self.properties(device or self.device(), DEVICE).get("ActiveConnection", "/")
        if path == "/":
            return {}, set()
        try:
            active = self.properties(path, ACTIVE)
            ip4 = active.get("Ip4Config", "/")
            if active.get("State") != STATE_ACTIVATED or ip4 == "/":
                return active, set()
            addresses = self.properties(ip4, NM + ".IP4Config").get("AddressData", [])
        except NetworkError as error:
            if str(error).endswith((".UnknownObject", ".UnknownMethod")):
                return {}, set()
            raise
        return active, {item["address"] for item in addresses
                        if not ipaddress.IPv4Address(item["address"]).is_link_local}

    def scan(self):
        paths = self.call(self.device(), WIRELESS, "GetAccessPoints")[0]
        networks = set()
        for path in paths:
            try:
                data = self.properties(path, NM + ".AccessPoint").get("Ssid", [])
                ssid = bytes(data).decode("utf-8")
                validate_wifi(ssid, "")
            except (ValueError, NetworkError):
                continue
            networks.add(ssid)
        return sorted(networks, key=str.casefold)

    def settings(self, ssid, password, role, *, hidden=False):
        validate_wifi(ssid, password)
        variant = self.glib.Variant
        hotspot = role == "hotspot"
        ipv4 = {"method": variant("s", "shared" if hotspot else "auto"),
                "may-fail": variant("b", False)}
        if hotspot:
            ipv4["address-data"] = variant("aa{sv}", [
                {"address": variant("s", HOTSPOT_ADDRESS), "prefix": variant("u", 24)}])
        settings = {
            "connection": {"id": variant("s", f"RaspiWiFi {role}: {ssid}"),
                           "uuid": variant("s", str(uuid.uuid4())),
                           "type": variant("s", WIFI),
                           "interface-name": variant("s", self.interface),
                           "autoconnect": variant("b", False)},
            "user": {"data": variant("a{ss}", {OWNER_KEY: self.owner, ROLE_KEY: role})},
            WIFI: {"ssid": variant("ay", list(ssid.encode("utf-8"))),
                   "mode": variant("s", "ap" if hotspot else "infrastructure")},
            "ipv4": ipv4,
            "ipv6": {"method": variant("s", "disabled" if hotspot else "auto")},
        }
        if hotspot:
            settings[WIFI]["band"] = variant("s", "bg")
            settings[WIFI]["ap-isolation"] = variant("i", 1)
        elif hidden:
            settings[WIFI]["hidden"] = variant("b", True)
        if password:
            settings[SECURITY] = {"key-mgmt": variant("s", "wpa-psk"),
                                  "psk": variant("s", password)}
            if hotspot:
                settings[SECURITY]["proto"] = variant("as", ["rsn"])
        return settings

    def add_hotspot(self, ssid, password):
        settings = self.settings(ssid, password, "hotspot")
        return self.call(ROOT + "/Settings", SETTINGS, "AddConnection", "(a{sa{sv}})", (settings,))[0]

    def delete(self, path):
        self.call(path, PROFILE, "Delete")

    def wait_for_ip(self, device, profile, expected_ip=None):
        deadline = time.monotonic() + ACTIVATION_TIMEOUT
        while time.monotonic() < deadline:
            active, addresses = self.active(device)
            if active.get("Connection") == profile:
                if addresses and (expected_ip is None or expected_ip in addresses):
                    return
                if active.get("State", 0) >= 3:
                    break
            state = self.properties(device, DEVICE).get("State", 0)
            if state == DEVICE_STATE_FAILED:
                break
            time.sleep(1)
        raise NetworkError("Connection failed or did not acquire an IPv4 address in time.")

    def activate_hotspot(self, profile):
        device = self.device()
        self.call(ROOT, NM, "ActivateConnection", "(ooo)", (profile, device, "/"))
        self.wait_for_ip(device, profile, HOTSPOT_ADDRESS)

    def switch(self, ssid, password, role, *, hidden=False, journal=None):
        """Test a candidate in memory before saving its credentials and committing the switch."""
        device = self.device()
        settings = self.settings(ssid, password, role, hidden=hidden)
        checkpoint = self.call(ROOT, NM, "CheckpointCreate", "(aouu)",
                               ([device], ACTIVATION_TIMEOUT + 30, 0))[0]
        candidate = None
        try:
            if journal is not None:
                write_json(journal, {"uuid": settings["connection"]["uuid"].unpack()})
            candidate, _, _ = self.call(ROOT, NM, "AddAndActivateConnection2", "(a{sa{sv}}ooa{sv})",
                                        (settings, device, "/", {"persist": self.glib.Variant("s", "volatile")}))
            self.wait_for_ip(device, candidate, HOTSPOT_ADDRESS if role == "hotspot" else None)
            settings["connection"]["autoconnect"] = self.glib.Variant("b", role == "client")
            self.call(ROOT, NM, "CheckpointDestroy", "(o)", (checkpoint,))
            checkpoint = None
            self.call(candidate, PROFILE, "Update2", "(a{sa{sv}}ua{sv})",
                      (settings, UPDATE_TO_DISK, {}))
        except (NetworkError, OSError):
            if checkpoint is not None:
                try:
                    results = self.call(ROOT, NM, "CheckpointRollback", "(o)", (checkpoint,))[0]
                    if results.get(device) != 0:
                        logging.error("NetworkManager could not restore the previous Wi-Fi state.")
                except NetworkError as error:
                    logging.error("Wi-Fi rollback: %s", error)
            if candidate is not None:
                try:
                    self.delete(candidate)
                except NetworkError as error:
                    if not str(error).endswith((".UnknownObject", ".UnknownMethod")):
                        logging.warning("Could not remove candidate Wi-Fi profile: %s", error)
            raise
        if journal is not None:
            journal.unlink(missing_ok=True)

        try:
            previous_profiles = self.owned_profiles(role)
        except NetworkError as error:
            logging.warning("Could not list previous RaspiWiFi profiles: %s", error)
            return
        ssid_bytes = list(ssid.encode("utf-8"))
        for path, existing in previous_profiles:
            same_ssid = existing.get(WIFI, {}).get("ssid") == ssid_bytes
            if path != candidate and (role == "hotspot" or same_ssid):
                try:
                    self.delete(path)
                except NetworkError as error:
                    logging.warning("Could not remove the previous RaspiWiFi profile: %s", error)

    def recover_interrupted_switch(self):
        if not PENDING_SWITCH.exists():
            return False
        candidate_uuid = json.loads(PENDING_SWITCH.read_text(encoding="utf-8"))["uuid"]
        for path, settings in self.owned_profiles():
            if settings["connection"]["uuid"] == candidate_uuid:
                if not self.properties(path, PROFILE).get("Unsaved"):
                    PENDING_SWITCH.unlink(missing_ok=True)
                    return False
                self.delete(path)
                break
        # Keep the recovery request durable until the setup hotspot works again.
        return True

