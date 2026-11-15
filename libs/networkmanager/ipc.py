from contextlib import contextmanager
import json
import os
from pathlib import Path
from queue import Full
import socket
from socketserver import StreamRequestHandler
import struct
from threading import Thread

from backend import validate_wifi


SOCKET_PATH = Path("/run/raspiwifi/control.sock")
MAX_MESSAGE = 4096


class PortalUnavailable(RuntimeError):
    pass


class RemoteController:
    def _call(self, operation):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(SOCKET_PATH))
                client.sendall((json.dumps(operation) + "\n").encode())
                with client.makefile("rb") as reply:
                    result = json.loads(reply.readline(262144))
        except (OSError, ValueError) as error:
            raise PortalUnavailable from error
        if result.get("error") == "busy":
            raise Full
        if "error" in result:
            raise PortalUnavailable
        return result

    @property
    def config(self):
        return self._call({"operation": "status"})

    @property
    def networks(self):
        return self.config["networks"]

    @property
    def protected(self):
        return self.config["protected"]

    @property
    def notice(self):
        return self.config["notice"]

    def submit(self, ssid, password, role, hidden):
        self._call({"operation": "change", "ssid": ssid, "password": password,
                    "role": role, "hidden": hidden})


@contextmanager
def control_socket(controller):
    from pwd import getpwnam
    from socketserver import UnixStreamServer

    account = getpwnam("raspiwifi-web")

    class Handler(StreamRequestHandler):
        timeout = 0.5

        def handle(self):
            credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != account.pw_uid:
                return
            try:
                data = self.rfile.readline(MAX_MESSAGE + 1)
                if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
                    return
                result = self.dispatch(json.loads(data))
            except (ValueError, TypeError, KeyError, OSError):
                result = {"error": "invalid"}
            except Full:
                result = {"error": "busy"}
            try:
                self.wfile.write((json.dumps(result) + "\n").encode())
            except OSError:
                pass

        def dispatch(self, data):
            if not isinstance(data, dict) or not controller.portal_active:
                return {"error": "inactive"}
            if data.get("operation") == "status":
                return {"ssid": controller.config["ssid"], "networks": controller.networks,
                        "protected": controller.protected, "notice": controller.notice}
            if data.get("operation") != "change":
                return {"error": "invalid"}
            ssid, password, role, hidden = (data[key] for key in ("ssid", "password", "role", "hidden"))
            if not isinstance(ssid, str) or not isinstance(password, str) or type(hidden) is not bool:
                return {"error": "invalid"}
            if role not in ("client", "hotspot"):
                return {"error": "invalid"}
            if role == "hotspot" and (not password or hidden or ssid != controller.config["ssid"]):
                return {"error": "invalid"}
            validate_wifi(ssid, password)
            controller.submit(ssid, password, role, hidden)
            return {"accepted": True}

    SOCKET_PATH.unlink(missing_ok=True)
    with UnixStreamServer(str(SOCKET_PATH), Handler) as server:
        os.chown(SOCKET_PATH, -1, account.pw_gid)
        SOCKET_PATH.chmod(0o660)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()
            SOCKET_PATH.unlink(missing_ok=True)
