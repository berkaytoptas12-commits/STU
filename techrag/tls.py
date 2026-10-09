"""TLS for the model endpoints in a closed network.

Internal vLLM/SGLang servers usually present a certificate from a company CA or a self-signed one, which the
CA list bundled with Python (certifi) does not know -> ``SSL: CERTIFICATE_VERIFY_FAILED``.

Trust sources, in order:
1. the Windows certificate store (company CAs deployed by IT) - via ``truststore`` when available;
2. certifi (public CAs);
3. extra CA / server certificate files from the settings (PEM or DER, ``;``-separated) - including server
   chains saved with "Trust this server", which may be a leaf or intermediate (partial chains are accepted).

Verification can be switched off per endpoint as a last resort.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import ssl
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

_CACHE: dict = {}
_LOCK = threading.Lock()

# Error codes shown to the user with a localised hint by the UI.
TLS_UNTRUSTED = "tls_untrusted"     # issuer not trusted (self-signed / company CA missing)
TLS_HOSTNAME = "tls_hostname"       # certificate issued for another name / IP
TLS_EXPIRED = "tls_expired"         # certificate expired or not yet valid
TLS_PROTOCOL = "tls_protocol"       # https:// used against a plain http server (or vice versa)
TLS_OTHER = "tls_other"
CONNECT = "connect"

HINTS = {
    TLS_UNTRUSTED: "The server certificate is not trusted (self-signed or issued by a company CA this PC does not "
                   "trust). Install the company root CA in Windows, add the CA file under Settings > TLS, or use "
                   "'Trust this server' after checking the fingerprint.",
    TLS_HOSTNAME: "The certificate was issued for a different host name/IP. Use the name written in the "
                  "certificate in the base URL (e.g. https://vllm.company.local:8000/v1 instead of the IP).",
    TLS_EXPIRED: "The server certificate has expired or is not yet valid (also check this PC's clock).",
    TLS_PROTOCOL: "TLS handshake failed: the server probably speaks plain HTTP. Try http:// instead of https://.",
    TLS_OTHER: "TLS connection failed.",
    CONNECT: "Cannot connect: check the address/port, that the server is running and the firewall.",
}


def _certifi_path() -> Optional[str]:
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def ca_files(spec: str) -> list[Path]:
    """Existing files from a ';' or newline separated list."""
    out = []
    for part in re.split(r"[;\n]", spec or ""):
        part = part.strip().strip('"')
        if part:
            p = Path(os.path.expandvars(part)).expanduser()
            if p.is_file():
                out.append(p)
    return out


def load_ca_file(ctx: ssl.SSLContext, path: Path) -> None:
    """PEM (one or many certificates) or DER (Windows '.cer' export)."""
    data = path.read_bytes()
    if b"-----BEGIN" in data:
        ctx.load_verify_locations(cadata=data.decode("ascii", "ignore"))
    else:
        ctx.load_verify_locations(cadata=data)


def build_context(system_store: bool = True, ca_bundle: str = "") -> ssl.SSLContext:
    files = ca_files(ca_bundle)
    key = (system_store, tuple((str(f), f.stat().st_mtime_ns) for f in files))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        ctx: Optional[ssl.SSLContext] = None
        if system_store and not files:
            try:  # Windows chain engine: company CAs, intermediates via AIA, ...
                import truststore

                ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                ctx = None
        if ctx is None:
            # create_default_context() loads the OS store (Windows ROOT + CA stores) when no cafile is given.
            ctx = ssl.create_default_context() if system_store else ssl.create_default_context(cafile=_certifi_path())
            if system_store and _certifi_path():
                ctx.load_verify_locations(cafile=_certifi_path())
            # A saved server certificate (leaf or intermediate) is a valid anchor; internal PKIs are often
            # missing extensions that the strict mode of newer Pythons rejects.
            ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
            ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
            for f in files:
                load_ca_file(ctx, f)
        _CACHE[key] = ctx
        return ctx


def httpx_verify(verify_ssl: bool, system_store: bool = True, ca_bundle: str = "") -> Union[bool, ssl.SSLContext]:
    return build_context(system_store, ca_bundle) if verify_ssl else False


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ----------------------------------------------------------------------------- diagnosis

def classify(exc: BaseException) -> str:
    texts = []
    verify_error = False
    e: Optional[BaseException] = exc
    for _ in range(6):
        if e is None:
            break
        verify_error |= isinstance(e, ssl.SSLCertVerificationError)
        texts.append(str(e))
        texts.append(getattr(e, "verify_message", "") or "")
        e = e.__cause__ or e.__context__
    low = " ".join(texts).lower()
    if verify_error or "certificate_verify_failed" in low or "certificate verify failed" in low:
        if any(k in low for k in ("hostname mismatch", "ip address mismatch", "doesn't match", "does not match",
                                  "cn name", "not valid for")):
            return TLS_HOSTNAME
        if any(k in low for k in ("expired", "not yet valid", "validity period")):
            return TLS_EXPIRED
        return TLS_UNTRUSTED
    if any(k in low for k in ("wrong_version_number", "wrong version number", "record layer failure",
                              "unknown protocol", "http_request", "packet length too long")):
        return TLS_PROTOCOL
    if "ssl" in low or "tls" in low:
        return TLS_OTHER
    return CONNECT


def explain(exc: BaseException, base_url: str) -> tuple[str, str]:
    """(code, message with hint) for a failed connection."""
    code = classify(exc)
    return code, f"cannot connect to {base_url}: {exc}. {HINTS[code]}"


# ----------------------------------------------------------------------------- server chain

@dataclass
class CertInfo:
    subject: str
    issuer: str
    not_before: str
    not_after: str
    sha256: str
    self_signed: bool
    names: list

    def to_dict(self) -> dict:
        return asdict(self)


def _name(rdns) -> str:
    return ", ".join(f"{k}={v}" for rdn in rdns or () for k, v in rdn)


def describe(der: bytes) -> CertInfo:
    sha = hashlib.sha256(der).hexdigest().upper()
    fingerprint = ":".join(sha[i:i + 2] for i in range(0, len(sha), 2))
    info: dict = {}
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
            fh.write(ssl.DER_cert_to_PEM_cert(der))
            tmp = fh.name
        try:
            info = ssl._ssl._test_decode_cert(tmp)  # type: ignore[attr-defined]
        finally:
            os.unlink(tmp)
    except Exception:
        info = {}
    subject, issuer = _name(info.get("subject")), _name(info.get("issuer"))
    names = [v for _, v in info.get("subjectAltName", ())]
    return CertInfo(subject, issuer, info.get("notBefore", ""), info.get("notAfter", ""), fingerprint,
                    bool(subject) and subject == issuer, names)


def host_port(base_url: str) -> tuple[str, int, str]:
    u = urlparse(base_url if "://" in base_url else "https://" + base_url)
    return u.hostname or "", u.port or (443 if u.scheme == "https" else 80), u.scheme


def fetch_chain(host: str, port: int, timeout: float = 10.0) -> list[bytes]:
    """DER certificates the server presents (leaf first), without verifying them."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            chain = None
            for getter in (getattr(s, "get_unverified_chain", None),
                           getattr(getattr(s, "_sslobj", None), "get_unverified_chain", None)):
                if getter is None:
                    continue
                try:
                    chain = getter()
                    break
                except Exception:
                    continue
            if not chain:
                leaf = s.getpeercert(binary_form=True)
                return [leaf] if leaf else []
    out = []
    for c in chain:
        out.append(bytes(c) if isinstance(c, (bytes, bytearray)) else c.public_bytes(2))  # 2 = ENCODING_DER
    return out


def check_handshake(host: str, port: int, ctx: ssl.SSLContext, timeout: float = 10.0) -> tuple[bool, str, str]:
    """(ok, code, message) of a verified handshake with the given trust settings."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return True, "", ""
    except Exception as exc:
        return False, classify(exc), str(exc)


def save_chain(chain: list[bytes], directory: Path, host: str, port: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", host)
    path = directory / f"{safe}_{port}.pem"
    path.write_text("".join(ssl.DER_cert_to_PEM_cert(c) for c in chain), encoding="ascii")
    return path
