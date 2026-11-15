from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
import os
from pathlib import Path
from secrets import token_urlsafe

from backend import HOTSPOT_ADDRESS
from config import CONFIG_DIR
from mdns import PORTAL_HOSTNAME


ADMIN_CODE = CONFIG_DIR / "admin-code"
PORTAL_CA = CONFIG_DIR / "portal-ca.pem"
PORTAL_CERT = CONFIG_DIR / "portal-cert.pem"
PORTAL_KEY = CONFIG_DIR / "portal-key.pem"


def provision():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    def store(path, data):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(data)

    paths = (ADMIN_CODE, PORTAL_CA, PORTAL_CERT, PORTAL_KEY)
    if all(path.is_file() for path in paths):
        return
    if any(path.exists() for path in paths):
        raise ValueError("Incomplete portal credentials. Restore or remove only the RaspiWiFi credential files.")

    def name(common_name):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    portal_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = name("RaspiWiFi " + token_urlsafe(8))
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.NameConstraints([x509.DNSName(PORTAL_HOSTNAME),
                                                    x509.IPAddress(ip_network(f"{HOTSPOT_ADDRESS}/32"))],
                                                   None), critical=True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None),
                              critical=True).sign(ca_key, hashes.SHA256()))
    portal_cert = (x509.CertificateBuilder().subject_name(name(PORTAL_HOSTNAME)).issuer_name(ca_name)
                   .public_key(portal_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(PORTAL_HOSTNAME),
                                                               x509.IPAddress(ip_address(HOTSPOT_ADDRESS))]),
                                  critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    files = {
        ADMIN_CODE: (token_urlsafe(32) + "\n").encode(),
        PORTAL_CA: ca_cert.public_bytes(serialization.Encoding.PEM),
        PORTAL_CERT: portal_cert.public_bytes(serialization.Encoding.PEM),
        PORTAL_KEY: portal_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()),
    }
    written = []
    try:
        for path, data in files.items():
            store(path, data)
            written.append(path)
    except OSError:
        for path in written:
            path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    provision()
