import re
import subprocess

from backend import HOTSPOT_ADDRESS, NetworkError


TABLE = "raspiwifi"


class HotspotFirewall:
    def __init__(self, interface, karaoke=False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface):
            raise ValueError("Invalid Wi-Fi interface name.")
        self.interface = f'"{interface}"'
        self.ports = "80, 443, 5555" if karaoke else "80, 443"
        self.enabled = None

    def update(self, enabled):
        if enabled == self.enabled:
            return
        rules = f"add table inet {TABLE}\n"
        if enabled:
            # Replace only our own table and keep the change atomic on service restart.
            rules += f"delete table inet {TABLE}\n" + f"""
table inet {TABLE} {{
    chain hotspot_input {{
        type filter hook input priority -10; policy accept;
        iifname {self.interface} udp sport 68 udp dport 67 accept
        iifname {self.interface} udp sport 67 udp dport 68 accept
        iifname {self.interface} ip daddr 224.0.0.251 udp dport 5353 accept
        iifname {self.interface} ip daddr {HOTSPOT_ADDRESS} udp dport {{ 53, 5353 }} accept
        iifname {self.interface} ip daddr {HOTSPOT_ADDRESS} tcp dport {{ 53, {self.ports} }} accept
        iifname {self.interface} drop
        ip daddr {HOTSPOT_ADDRESS} drop
    }}
    chain hotspot_forward {{
        type filter hook forward priority -10; policy accept;
        iifname {self.interface} drop
        oifname {self.interface} drop
    }}
}}
"""
        else:
            rules += f"delete table inet {TABLE}\n"
        result = subprocess.run(["/usr/sbin/nft", "-f", "-"], input=rules, text=True, capture_output=True)
        if result.returncode:
            raise NetworkError(f"Could not apply the RaspiWiFi firewall: {result.stderr.strip()}")
        self.enabled = enabled
