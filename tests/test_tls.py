"""HTTPS trust against a real local TLS server: company CA, DER files, self-signed servers, partial chains,
hostname mismatch, plain-HTTP servers and the 'trust this server' flow."""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from techrag import tls
from techrag.api import check_service, configure_tls
from techrag.config import ServiceConfig, TLSConfig

CERTS = Path(__file__).parent / "certs"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": [{"id": "qwen-test"}]}).encode()
        self.send_response(200 if self.path.endswith("/models") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def serve():
    servers = []

    def start(cert: str | None) -> int:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERTS / f"{cert}.pem", CERTS / f"{cert}.key")
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    yield start
    for s in servers:
        s.shutdown()


@pytest.fixture(autouse=True)
def _reset_tls():
    configure_tls(TLSConfig())
    yield
    configure_tls(TLSConfig())


def svc(port: int, scheme: str = "https", host: str = "127.0.0.1", **kw) -> ServiceConfig:
    return ServiceConfig(base_url=f"{scheme}://{host}:{port}/v1", model="qwen-test", timeout=10, **kw)


def test_untrusted_company_ca_is_reported_with_hint(serve):
    r = check_service(svc(serve("server")))
    assert not r["ok"] and r["code"] == tls.TLS_UNTRUSTED
    assert "not trusted" in r["error"] and "Trust this server" in r["error"]


@pytest.mark.parametrize("ca_file", ["ca.pem", "ca.cer"])
def test_company_ca_file_pem_or_der(serve, ca_file):
    configure_tls(TLSConfig(ca_bundle=f"  {CERTS / 'missing.pem'} ; {CERTS / ca_file} "))
    r = check_service(svc(serve("server")))
    assert r["ok"], r


def test_system_store_off_still_uses_extra_ca(serve):
    configure_tls(TLSConfig(system_store=False, ca_bundle=str(CERTS / "ca.pem")))
    assert check_service(svc(serve("server")))["ok"]


def test_trust_self_signed_server_via_saved_chain(serve, tmp_path):
    port = serve("self")
    assert check_service(svc(port))["code"] == tls.TLS_UNTRUSTED
    chain = tls.fetch_chain("127.0.0.1", port)
    info = tls.describe(chain[0])
    assert info.self_signed and "localhost" in info.names and len(info.sha256.split(":")) == 32
    saved = tls.save_chain(chain, tmp_path, "127.0.0.1", port)
    configure_tls(TLSConfig(ca_bundle=str(saved)))
    assert check_service(svc(port))["ok"]


def test_pinning_a_ca_signed_leaf_works_without_the_root(serve, tmp_path):
    port = serve("server")
    chain = tls.fetch_chain("127.0.0.1", port)
    assert not tls.describe(chain[0]).self_signed
    configure_tls(TLSConfig(ca_bundle=str(tls.save_chain(chain[:1], tmp_path, "h", port))))
    assert check_service(svc(port))["ok"], "partial chain: a trusted leaf is a valid anchor"


def test_hostname_mismatch(serve):
    configure_tls(TLSConfig(ca_bundle=str(CERTS / "ca.pem")))
    r = check_service(svc(serve("wrong")))
    assert not r["ok"] and r["code"] == tls.TLS_HOSTNAME and "different host name" in r["error"]


def test_https_against_plain_http_server(serve):
    r = check_service(svc(serve(None), scheme="https"))
    assert not r["ok"] and r["code"] == tls.TLS_PROTOCOL and "http://" in r["error"]


def test_verification_can_be_disabled_per_endpoint(serve):
    assert check_service(svc(serve("self"), verify_ssl=False))["ok"]


def test_handshake_check_and_context_cache(serve):
    port = serve("server")
    ok, code, _ = tls.check_handshake("127.0.0.1", port, tls.build_context(True, ""))
    assert not ok and code == tls.TLS_UNTRUSTED
    ctx = tls.build_context(True, str(CERTS / "ca.pem"))
    assert tls.build_context(True, str(CERTS / "ca.pem")) is ctx
    assert tls.check_handshake("127.0.0.1", port, ctx)[0]


def test_classify_windows_messages():
    class E(ssl.SSLCertVerificationError):
        pass

    assert tls.classify(E("The certificate's CN name does not match the passed value.")) == tls.TLS_HOSTNAME
    assert tls.classify(E("A required certificate is not within its validity period")) == tls.TLS_EXPIRED
    assert tls.classify(E("terminated in a root certificate which is not trusted")) == tls.TLS_UNTRUSTED
    assert tls.classify(ConnectionRefusedError("refused")) == tls.CONNECT


def test_llm_client_reports_tls_code(serve):
    from techrag.config import LLMConfig
    from techrag.llm import LLMClient, LLMError

    port = serve("self")
    client = LLMClient(LLMConfig(base_url=f"https://127.0.0.1:{port}/v1", model="m"))
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert ei.value.code == tls.TLS_UNTRUSTED


def test_settings_endpoints_inspect_and_trust(serve, cfg, engine, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from techrag.server import create_app
    from techrag.settings import load_user_settings

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    port = serve("self")
    base = f"https://127.0.0.1:{port}/v1"
    with TestClient(create_app(cfg, engine, warmup=False)) as c:
        r = c.post("/api/settings/models", json={"service": "llm", "base_url": base}).json()
        assert not r["ok"] and r["code"] == tls.TLS_UNTRUSTED

        info = c.post("/api/settings/certificate", json={"base_url": base}).json()
        assert info["verify"]["ok"] is False and info["chain"][0]["self_signed"]
        fp = info["chain"][0]["sha256"]

        assert c.post("/api/settings/trust", json={"base_url": base, "sha256": "00:11"}).status_code == 409
        r = c.post("/api/settings/trust", json={"base_url": base, "sha256": fp}).json()
        assert r["ok"] and Path(r["path"]).exists()
        assert str(r["path"]) in load_user_settings()["tls"]["ca_bundle"]

        r = c.post("/api/settings/models", json={"service": "llm", "base_url": base}).json()
        assert r["ok"] and r["models"] == ["qwen-test"]
        assert c.post("/api/settings/certificate", json={"base_url": "http://127.0.0.1:1/v1"}).status_code == 400


def test_cli_cert_inspect_and_trust(serve, tmp_path, monkeypatch, capsys):
    from techrag.cli import main
    from techrag.settings import load_user_settings

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    port = serve("self")
    url = f"https://127.0.0.1:{port}/v1"
    assert main(["cert", url, "--save", str(tmp_path / "chain.pem")]) == 1
    out = capsys.readouterr().out
    assert "(self-signed)" in out and "FAILED" in out and (tmp_path / "chain.pem").exists()
    assert main(["cert", url, "--trust", "--yes"]) == 0
    assert "certs" in load_user_settings()["tls"]["ca_bundle"]
    assert main(["cert", url]) == 0 and "verification with current settings: OK" in capsys.readouterr().out
