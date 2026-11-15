import logging
from queue import Empty, Full, Queue
import subprocess
from threading import Lock
import time

from backend import (
    HOTSPOT_ADDRESS, SECURITY, WIFI, NetworkError, NetworkManager,
)
from config import PENDING_SWITCH, RESET_REQUEST, load_config
from firewall import HotspotFirewall
from ipc import control_socket
from mdns import PortalMdns


class Controller:
    def __init__(self, config, network, mdns=None, firewall=None, journal=None):
        self.config = config
        self.network = network
        self.mdns = mdns
        self.requests = Queue(maxsize=1)
        self.change_pending = Lock()
        self.notice = ""
        self.networks = []
        self.protected = False
        self.restore_hotspot = False
        self.disconnected_since = time.monotonic()
        self.last_scan = float("-inf")
        self.portal_active = False
        self.firewall = firewall
        self.journal = journal

    def submit(self, ssid, password, role, hidden):
        if not self.change_pending.acquire(blocking=False):
            raise Full
        self.requests.put_nowait((time.monotonic() + 2, ssid, password, role, hidden))

    def refresh_networks(self, *, force=False):
        now = time.monotonic()
        if force or now - self.last_scan >= 15:
            self.last_scan = now
            try:
                self.networks = self.network.scan() or self.networks
            except NetworkError:
                # Some drivers do not provide scan results while hosting an AP.
                pass

    def update_portal(self, is_hotspot):
        if self.portal_active != is_hotspot:
            action = "start" if is_hotspot else "stop"
            result = subprocess.run(["/usr/bin/systemctl", "--no-block", action,
                                     "raspiwifi-landing.service", "raspiwifi-web@https.service"],
                                    capture_output=True)
            if result.returncode:
                raise NetworkError(f"Could not {action} the portal services.")
            self.portal_active = is_hotspot
        if self.mdns is not None:
            self.mdns.update(is_hotspot)

    def step(self):
        profiles = self.network.owned_profiles("hotspot")
        if not profiles:
            raise NetworkError("No RaspiWiFi hotspot profile. Run initial_setup.py to restore it.")
        active, addresses = self.network.active()
        active_profile = active.get("Connection")
        owned_hotspot = next((settings for path, settings in profiles if path == active_profile), None)
        is_hotspot = owned_hotspot is not None and HOTSPOT_ADDRESS in addresses
        if is_hotspot:
            self.protected = SECURITY in owned_hotspot
        if addresses and not is_hotspot:
            settings = self.network.profile_settings(active_profile)
            if settings.get(WIFI, {}).get("mode", "infrastructure") == "infrastructure":
                self.disconnected_since = time.monotonic()
                self.refresh_networks()

        requested = RESET_REQUEST.exists()
        fallback = self.config["auto_config"] and not addresses and (
            time.monotonic() - self.disconnected_since >= self.config["delay"])
        if not is_hotspot and (requested or fallback or self.restore_hotspot):
            self.refresh_networks(force=True)
            if self.firewall is not None:
                self.firewall.update(True)
            try:
                self.network.activate_hotspot(profiles[0][0])
            except NetworkError:
                if self.firewall is not None and self.network.active()[0].get("Connection") == active_profile and addresses:
                    self.firewall.update(False)
                raise
            self.disconnected_since = time.monotonic()
            is_hotspot = True
            self.protected = SECURITY in profiles[0][1]
        if is_hotspot and requested:
            RESET_REQUEST.unlink(missing_ok=True)
        if is_hotspot:
            self.restore_hotspot = False
            if self.journal is not None:
                self.journal.unlink(missing_ok=True)

        if self.firewall is not None:
            self.firewall.update(is_hotspot)
        self.update_portal(is_hotspot)
        if not is_hotspot:
            try:
                self.requests.get_nowait()
            except Empty:
                pass
            else:
                self.change_pending.release()
            return

        self.refresh_networks()
        try:
            ready, ssid, password, role, hidden = self.requests.get_nowait()
        except Empty:
            return
        time.sleep(max(0, ready - time.monotonic()))
        if self.mdns is not None:
            self.mdns.update(False)
        try:
            self.network.switch(ssid, password, role, hidden=hidden, journal=self.journal)
        except (NetworkError, OSError) as error:
            self.notice = ("Não foi possível conectar. Confira o nome da rede e a senha e tente novamente."
                           if role == "client" else
                           "Não foi possível atualizar o hotspot. Confira os dados e tente novamente.")
            self.restore_hotspot = True
            logging.warning("Wi-Fi change failed: %s", error)
        else:
            self.notice = ""
            self.disconnected_since = time.monotonic()
            if role == "hotspot":
                self.protected = bool(password)
            self.update_portal(role == "hotspot")
            if self.firewall is not None and role == "client":
                self.firewall.update(False)
        finally:
            self.change_pending.release()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config()
    interface = config["interface"]
    network = NetworkManager(interface, config["owner"])
    controller = Controller(config, network,
                            PortalMdns(interface, HOTSPOT_ADDRESS),
                            HotspotFirewall(interface, config.get("karaoke", False)), PENDING_SWITCH)
    controller.restore_hotspot = network.recover_interrupted_switch()
    with control_socket(controller):
        while True:
            try:
                controller.step()
            except NetworkError as error:
                logging.warning("RaspiWiFi: %s", error)
            time.sleep(3)


if __name__ == "__main__":
    main()
