"""Throwaway test CA and leaf certificates (M8a tests and smoke only).

Keys exist only in the test's temporary directory or the smoke context
outside the repo; nothing here is trusted by the host or any real browser
profile.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class Pem:
    cert: Path
    key: Path


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RAS synthetic test CA")])


def _write(directory: Path, stem: str, cert: x509.Certificate, key) -> Pem:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / f"{stem}.crt", directory / f"{stem}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return Pem(cert_path, key_path)


def make_ca(directory: Path, stem: str = "ca", common: str = "RAS synthetic test root") -> Pem:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(_name(common)).issuer_name(_name(common))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return _write(directory, stem, cert, key)


def make_leaf(directory: Path, ca: Pem, stem: str, names: list[str], *, days: int = 7) -> Pem:
    ca_cert = x509.load_pem_x509_certificate(ca.cert.read_bytes())
    ca_key = serialization.load_pem_private_key(ca.key.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    alt = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    cert = (x509.CertificateBuilder().subject_name(_name(names[0])).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return _write(directory, stem, cert, key)
