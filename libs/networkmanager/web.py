from datetime import timedelta
import hashlib
from ipaddress import IPv4Address, IPv4Network
import os
from pathlib import Path
from queue import Full
from secrets import compare_digest, token_hex
import sys

from flask import Flask, abort, redirect, render_template, request, session, url_for

from backend import (HOTSPOT_ADDRESS, HOTSPOT_PASSWORD_MIN_LENGTH, WiFiValidationError,
                     validate_hotspot_password, validate_wifi)
from ipc import RemoteController, PortalUnavailable
from mdns import PORTAL_HOSTNAME


WEB_ROOT = Path(__file__).resolve().parents[1] / "configuration_app"
HOTSPOT_NETWORK = IPv4Network(f"{HOTSPOT_ADDRESS}/24", strict=False)
PASSWORD_ERROR = "Use de 8 a 63 caracteres ASCII imprimíveis ou uma chave hexadecimal de 64 dígitos."
HOTSPOT_PASSWORD_RULE = (
    f"Use de {HOTSPOT_PASSWORD_MIN_LENGTH} a 63 caracteres ASCII imprimíveis "
    "ou uma chave hexadecimal de 64 dígitos."
)
CHANGE_PENDING = "Já existe uma alteração em andamento. Aguarde e tente novamente."


def create_app(controller, admin_code, *, landing=False):
    app = Flask(__name__, template_folder=str(WEB_ROOT / "templates"),
                static_folder=str(WEB_ROOT / "static"))
    app.config["MAX_CONTENT_LENGTH"] = 4096
    app.secret_key = hashlib.sha256(b"raspiwifi session\0" + admin_code.encode()).digest()
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_NAME="__Host-raspiwifi",
                      PERMANENT_SESSION_LIFETIME=timedelta(minutes=15))

    @app.context_processor
    def portal_context():
        if not request.is_secure:
            return {}
        notice = controller.notice if session.get("authorized") else ""
        return {"csrf_token": session.setdefault("csrf", token_hex(32)), "notice": notice,
                "hotspot_password_rule": HOTSPOT_PASSWORD_RULE}

    @app.before_request
    def check_portal_request():
        # Binding the AP address limits the listener. Also reject cross-network
        # requests and Host headers that could be used for DNS rebinding.
        hostname, separator, port = request.host.lower().partition(":")
        hostname = hostname.removesuffix(".")
        if hostname not in (HOTSPOT_ADDRESS, PORTAL_HOSTNAME):
            abort(403)
        if separator and port != ("443" if request.is_secure else "80"):
            abort(403)
        try:
            peer = IPv4Address(request.remote_addr)
        except (ValueError, TypeError):
            abort(403)
        if peer not in HOTSPOT_NETWORK:
            abort(403)
        if landing:
            if request.method not in ("GET", "HEAD"):
                abort(405)
            if request.endpoint == "static":
                return
            return render_template("networkmanager/landing.html", portal_url=f"https://{hostname}/")
        if not request.is_secure:
            abort(403)
        if request.method == "POST" and ("csrf" not in session or not compare_digest(
                request.form.get("csrf_token", "").encode(), session["csrf"].encode())):
            abort(403)
        if request.endpoint not in ("login", "static") and not session.get("authorized"):
            if request.method == "POST":
                abort(403)
            return redirect(url_for("login"))

    @app.errorhandler(PortalUnavailable)
    def unavailable(error):
        return "O serviço de configuração está indisponível. Tente novamente.", 503

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if not compare_digest(request.form.get("admin_code", "").encode(), admin_code.encode()):
                return render_template("networkmanager/login.html", error=True), 401
            session.clear()
            session.permanent = True
            session["authorized"] = True
            return redirect(url_for("index"), code=303)
        return render_template("networkmanager/login.html")

    @app.after_request
    def prevent_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return response

    def connection_form(*, manual=False, ssid="", hidden=False, errors=None):
        networks = list(controller.networks)
        if ssid and not manual and ssid not in networks:
            networks.insert(0, ssid)
        return render_template("networkmanager/connect.html", networks=networks, ssid=ssid,
                               manual=manual, hidden=hidden, protected=controller.protected,
                               errors=errors or {})

    @app.get("/")
    def index():
        return connection_form()

    @app.get("/manual_ssid_entry")
    def manual_ssid_entry():
        return connection_form(manual=True)

    @app.get("/wpa_settings")
    def wpa_settings():
        return render_template("networkmanager/hotspot.html", protected=controller.protected, errors={})

    @app.post("/save_credentials")
    def save_credentials():
        ssid = request.form.get("ssid", "")
        password = request.form.get("wifi_key", "")
        hidden = request.form.get("hidden") == "1"
        manual = hidden or request.form.get("manual") == "1"
        try:
            validate_wifi(ssid, password)
            controller.submit(ssid, password, "client", hidden)
        except WiFiValidationError as error:
            message = PASSWORD_ERROR if error.field == "password" else (
                "Digite um nome de rede de até 32 bytes." if manual else "Selecione uma rede Wi-Fi.")
            return connection_form(manual=manual, ssid=ssid, hidden=hidden,
                                   errors={error.field: message}), 400
        except Full:
            return connection_form(manual=manual, ssid=ssid, hidden=hidden,
                                   errors={"form": CHANGE_PENDING}), 409
        return render_template("networkmanager/transition.html", ssid=ssid,
                               portal_url=url_for("index", _external=True)), 202

    @app.post("/save_wpa_credentials")
    def save_wpa_credentials():
        enabled = request.form.get("wpa_enabled") == "1"
        password = request.form.get("wpa_key", "")
        if not enabled:
            abort(400)
        if enabled and not password:
            return render_template("networkmanager/hotspot.html", protected=True,
                                   errors={"password": "Digite a senha do hotspot."}), 400
        try:
            validate_hotspot_password(password)
            controller.submit(controller.config["ssid"], password, "hotspot", False)
        except WiFiValidationError:
            return render_template("networkmanager/hotspot.html", protected=enabled,
                                   errors={"password": HOTSPOT_PASSWORD_RULE}), 400
        except Full:
            return render_template("networkmanager/hotspot.html", protected=enabled,
                                   errors={"form": CHANGE_PENDING}), 409
        return render_template("networkmanager/transition.html", hotspot=True, protected=enabled,
                               portal_url=url_for("index", _external=True)), 202

    return app


def main():
    from gunicorn.app.base import BaseApplication

    mode = sys.argv[1]
    if mode not in ("http", "https"):
        raise ValueError("Expected http or https.")
    if mode == "https":
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        app = create_app(RemoteController(), (credentials / "admin").read_text().strip())
    else:
        app = create_app(None, "", landing=True)

    class PortalServer(BaseApplication):
        def load_config(self):
            options = {"bind": f"{HOTSPOT_ADDRESS}:{80 if mode == 'http' else 443}",
                       "workers": 1, "threads": 4, "timeout": 15, "graceful_timeout": 3}
            if mode == "https":
                options.update(certfile=str(credentials / "cert"), keyfile=str(credentials / "key"))
            for key, value in options.items():
                self.cfg.set(key, value)

        def load(self):
            return app

    PortalServer().run()


if __name__ == "__main__":
    main()
