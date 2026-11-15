from pathlib import Path
from queue import Full
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "networkmanager"))
from backend import HOTSPOT_ADDRESS, WIFI, NetworkError, NetworkManager
import service
import web


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.network = NetworkManager.__new__(NetworkManager)
        self.network.interface = "wlan0"
        self.network.owner = "test"
        self.network.glib = Mock()
        self.network.glib.Variant.side_effect = lambda signature, value: value
        self.network.device = Mock(return_value="/device")
        self.network.wait_for_ip = Mock()
        self.network.owned_profiles = Mock(return_value=[
            ("/old", {WIFI: {"ssid": list(b"venue")}}),
            ("/other", {WIFI: {"ssid": list(b"other")}}),
        ])
        self.methods = []
        self.saved = None

        def call(path, interface, method, signature=None, values=()):
            self.methods.append(method)
            if method == "CheckpointCreate":
                return ("/checkpoint",)
            if method == "AddAndActivateConnection2":
                self.assertEqual(values[3], {"persist": "volatile"})
                self.assertFalse(values[0]["connection"]["autoconnect"])
                return "/candidate", "/active", {}
            if method == "CheckpointRollback":
                return ({"/device": 0},)
            if method == "Update2":
                self.network.wait_for_ip.assert_called_once_with("/device", "/candidate", None)
                self.saved = values[0]
            if method == "Delete":
                self.assertIn(path, ("/old", "/candidate"))
            return ()

        self.network.call = call

    def test_persists_client_only_after_ipv4_is_ready(self):
        self.network.switch("venue", "test-pass", "client", hidden=True)
        self.assertEqual(self.methods, ["CheckpointCreate", "AddAndActivateConnection2",
                                        "CheckpointDestroy", "Update2", "Delete"])
        self.assertTrue(self.saved["connection"]["autoconnect"])
        self.assertTrue(self.saved[WIFI]["hidden"])

    def test_failed_client_restores_hotspot_and_discards_candidate(self):
        self.network.wait_for_ip.side_effect = NetworkError("no IPv4")
        with self.assertRaisesRegex(NetworkError, "no IPv4"):
            self.network.switch("venue", "test-pass", "client")
        self.assertEqual(self.methods, ["CheckpointCreate", "AddAndActivateConnection2",
                                        "CheckpointRollback", "Delete"])
        self.network.owned_profiles.assert_not_called()

    def test_restart_discards_only_its_uncommitted_profile(self):
        self.network.owned_profiles.return_value = [
            ("/candidate", {"connection": {"uuid": "pending"}}),
            ("/existing", {"connection": {"uuid": "existing"}}),
        ]
        self.network.properties = Mock(return_value={"Unsaved": True})
        self.network.delete = Mock()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "pending.json"
            journal.write_text('{"uuid": "pending"}', encoding="utf-8")
            with patch("backend.PENDING_SWITCH", journal):
                self.assertTrue(self.network.recover_interrupted_switch())
                self.network.delete.assert_called_once_with("/candidate")
                self.assertTrue(journal.exists())


class PortalTests(unittest.TestCase):
    def setUp(self):
        config = {"ssid": "setup", "auto_config": True, "delay": 300, "port": 80, "tls": False}
        self.network = Mock()
        self.network.owned_profiles.return_value = [("/hotspot", {})]
        self.network.active.return_value = ({"Connection": "/hotspot"}, {HOTSPOT_ADDRESS})
        self.controller = service.Controller(config, self.network)
        self.controller.update_portal = Mock()
        self.controller.networks = ['Venue: "Wi-Fi"']
        self.local = {"REMOTE_ADDR": "10.0.0.42"}
        self.url = "https://10.0.0.1"
        self.admin_code = "test-administrative-code-only"
        self.app = web.create_app(self.controller, self.admin_code)
        self.client = self.app.test_client()
        self.login(self.url)

    def login(self, url):
        kwargs = {"base_url": url, "environ_overrides": self.local}
        page = self.client.get("/login", **kwargs)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        login = self.client.post("/login", data={"admin_code": self.admin_code, "csrf_token": token}, **kwargs)
        self.assertEqual(login.status_code, 303)
        cookie = login.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_form_keeps_special_ssid_and_allows_one_pending_change(self):
        page = self.client.get("/", base_url=self.url, environ_overrides=self.local)
        self.assertEqual(page.status_code, 200)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        payload = {"csrf_token": token, "ssid": 'Venue: "Wi-Fi"', "wifi_key": "test-pass"}
        kwargs = {"base_url": self.url, "environ_overrides": self.local}
        self.assertEqual(self.client.post("/save_credentials", data=payload, **kwargs).status_code, 202)
        self.assertEqual(self.client.post("/save_credentials", data=payload, **kwargs).status_code, 409)
        self.assertEqual(self.client.post("/save_credentials", data={}, **kwargs).status_code, 403)
        self.assertEqual(self.client.get("/", base_url="https://other.local",
                                         environ_overrides=self.local).status_code, 403)

    def test_mdns_hostname_allows_portal_access_only_from_hotspot(self):
        url = "https://acesso.local"
        self.client = self.app.test_client()
        self.login(url)
        kwargs = {"base_url": url, "environ_overrides": self.local}
        page = self.client.get("/", **kwargs)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.client.get("/static/portal.js", **kwargs).status_code, 200)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        form = {"csrf_token": token, "ssid": 'Venue: "Wi-Fi"', "wifi_key": "test-pass"}
        result = self.client.post("/save_credentials", data=form, **kwargs)
        self.assertEqual(result.status_code, 202)
        self.assertIn(b'href="https://acesso.local/"', result.data)
        self.assertEqual(self.client.get("/", base_url=url,
                                         environ_overrides={"REMOTE_ADDR": "192.168.1.2"}).status_code, 403)

    def test_guest_and_http_cannot_submit_changes(self):
        anonymous = self.app.test_client()
        kwargs = {"base_url": self.url, "environ_overrides": self.local}
        page = anonymous.get("/login", **kwargs)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        form = {"csrf_token": token, "ssid": "Fixture", "wifi_key": "test-pass"}
        for route in ("/save_credentials", "/save_wpa_credentials"):
            self.assertEqual(anonymous.post(route, data=form, **kwargs).status_code, 403)

        landing = web.create_app(self.controller, self.admin_code, landing=True).test_client()
        kwargs["base_url"] = "http://acesso.local"
        response = landing.get("/", **kwargs)
        self.assertIn(b'href="https://acesso.local/"', response.data)
        self.assertNotIn(b"<form", response.data)
        self.assertEqual(landing.post("/save_credentials", data=form, **kwargs).status_code, 405)
        self.assertTrue(self.controller.requests.empty())

    def test_failed_switch_leaves_portal_available_for_retry(self):
        self.controller.submit("venue", "test-pass", "client", False)
        with self.assertRaises(Full):
            self.controller.submit("other", "test-pass", "client", False)
        self.network.switch.side_effect = NetworkError("failed")
        self.network.scan.return_value = []
        with patch.object(service.RESET_REQUEST.__class__, "exists", return_value=False), \
             patch.object(service.time, "sleep"), self.assertLogs(level="WARNING"):
            self.controller.step()
        self.controller.update_portal.assert_called_once_with(True)
        self.assertIn("Não foi possível conectar", self.controller.notice)
        self.controller.submit("other", "test-pass", "client", False)

    def test_invalid_password_keeps_network_selection_and_clears_password(self):
        kwargs = {"base_url": self.url, "environ_overrides": self.local}
        page = self.client.get("/", **kwargs)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        payload = {"csrf_token": token, "ssid": 'Venue: "Wi-Fi"', "wifi_key": "short"}
        result = self.client.post("/save_credentials", data=payload, **kwargs)
        self.assertEqual(result.status_code, 400)
        self.assertIn(b'<option value="Venue: &#34;Wi-Fi&#34;" selected>', result.data)
        self.assertIn(b'id="wifi_key-error"', result.data)
        self.assertNotIn(b"short", result.data)
        self.assertTrue(self.controller.requests.empty())

    def test_invalid_manual_submission_keeps_hidden_network_choice(self):
        kwargs = {"base_url": self.url, "environ_overrides": self.local}
        page = self.client.get("/manual_ssid_entry", **kwargs)
        token = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)[1].decode()
        payload = {"csrf_token": token, "ssid": "", "manual": "1", "hidden": "1"}
        result = self.client.post("/save_credentials", data=payload, **kwargs)
        self.assertEqual(result.status_code, 400)
        self.assertIn(b'name="hidden" value="1" checked', result.data)
        self.assertIn(b'id="ssid-error"', result.data)

    def test_automatic_fallback_waits_for_saved_profile(self):
        self.network.active.return_value = ({}, set())
        self.network.scan.return_value = []
        with patch.object(service.RESET_REQUEST.__class__, "exists", return_value=False), \
             patch.object(service.time, "monotonic", return_value=self.controller.disconnected_since + 299):
            self.controller.step()
        self.network.activate_hotspot.assert_not_called()
        with patch.object(service.RESET_REQUEST.__class__, "exists", return_value=False), \
             patch.object(service.time, "monotonic", return_value=self.controller.disconnected_since + 301):
            self.controller.step()
        self.network.activate_hotspot.assert_called_once_with("/hotspot")

    def test_restore_hotspot_after_failed_rollback_even_with_auto_config_disabled(self):
        self.controller.config["auto_config"] = False
        self.controller.restore_hotspot = True
        self.network.active.return_value = ({}, set())
        self.network.scan.return_value = []
        with patch.object(service.RESET_REQUEST.__class__, "exists", return_value=False):
            self.controller.step()
        self.network.activate_hotspot.assert_called_once_with("/hotspot")
        self.controller.update_portal.assert_called_once_with(True)
        self.assertFalse(self.controller.restore_hotspot)


if __name__ == "__main__":
    unittest.main()
