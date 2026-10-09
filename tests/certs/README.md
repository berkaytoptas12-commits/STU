Test-only certificates (100-year validity) for tests/test_tls.py. Never use them elsewhere.
- ca.pem / ca.cer : test root CA (PEM and DER)
- server.pem/.key : localhost + 127.0.0.1, signed by the test CA
- wrong.pem/.key  : wrong.example, signed by the test CA (hostname mismatch)
- self.pem/.key   : self-signed localhost + 127.0.0.1
