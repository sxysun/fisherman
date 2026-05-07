import re
import tempfile
import unittest
from pathlib import Path

from mirror.server import _fingerprint_leaf_cert_pem


class MirrorTlsFingerprintTests(unittest.TestCase):
    def test_fingerprints_first_pem_certificate(self):
        bundle = Path(__file__).resolve().parents[1] / "server" / "certs" / "global-bundle.pem"
        data = bundle.read_bytes()
        begin = b"-----BEGIN CERTIFICATE-----"
        end = b"-----END CERTIFICATE-----"
        start = data.find(begin)
        stop = data.find(end, start)
        first_pem = data[start:stop + len(end)] + b"\n"

        with tempfile.TemporaryDirectory() as td:
            cert_path = Path(td) / "cert.pem"
            cert_path.write_bytes(first_pem)

            fp = _fingerprint_leaf_cert_pem(str(cert_path))

        self.assertRegex(fp, re.compile(r"^[0-9a-f]{64}$"))


if __name__ == "__main__":
    unittest.main()
