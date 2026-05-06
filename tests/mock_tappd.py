"""Mock dstack tappd.sock for end-to-end fisherman testing.

Listens on a Unix-domain socket and answers the two prpc methods the
mirror calls — `Tappd.Info` and `Tappd.TdxQuote` — using the real-quote
fixture under tests/fixtures/. The fixture is a Phala-dstack-simulator
quote, so the body sig is known to fail by design (matches feedling-
mcp-v1's behaviour); every other check should pass.

Run as:

    uv run python3 tests/mock_tappd.py /tmp/fish-tappd.sock

then in another shell:

    DSTACK_TAPPD_SOCK=/tmp/fish-tappd.sock \
      uv run python3 -m mirror.server
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_QUOTE_HEX = (FIXTURES / "sample_dstack_quote.hex").read_text().strip()
SAMPLE_BUNDLE    = json.loads((FIXTURES / "sample_dstack_bundle.json").read_text())


def _make_response(body: dict) -> bytes:
    payload = json.dumps(body).encode()
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n"
    )
    return head + payload


def _info_response() -> bytes:
    meas = SAMPLE_BUNDLE["measurements"]
    # Build an mr_config_id with the dstack 0x01 + compose_hash binding
    # so the audit's mr_config_id row goes green too. The real fixture
    # bundle was Phase-1 (no binding), so we synthesize one here for
    # the mock — that's fine because the test target is the mirror →
    # auditor pipeline, not the fixture's authenticity.
    compose_hash_hex = SAMPLE_BUNDLE["compose_hash"]
    mr_config_id_hex = "01" + compose_hash_hex + "00" * 15
    return _make_response({
        "app_id": SAMPLE_BUNDLE["app_id"],
        "instance_id": SAMPLE_BUNDLE["instance_id"],
        "app_name": "fisherman-mirror-test",
        "tcb_info": json.dumps({
            "compose_hash": compose_hash_hex,
            "mr_config_id": mr_config_id_hex,
            "mrtd":  meas["mrtd"],
            "rtmr0": meas["rtmr0"],
            "rtmr1": meas["rtmr1"],
            "rtmr2": meas["rtmr2"],
            "rtmr3": meas["rtmr3"],
            "event_log": SAMPLE_BUNDLE["event_log_json"],
        }),
    })


def _quote_response(req_body: bytes) -> bytes:
    return _make_response({
        "quote": SAMPLE_QUOTE_HEX,
        "event_log": SAMPLE_BUNDLE["event_log_json"],
    })


def _handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(5.0)
        chunks: list[bytes] = []
        while True:
            buf = conn.recv(4096)
            if not buf:
                break
            chunks.append(buf)
            raw = b"".join(chunks)
            head, sep, body = raw.partition(b"\r\n\r\n")
            if not sep:
                continue
            # Wait for full body if Content-Length present.
            cl = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        cl = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        cl = 0
            if len(body) >= cl:
                break

        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        try:
            _method, path, _proto = request_line.split(" ", 2)
        except ValueError:
            conn.sendall(_make_response({"error": "bad_request"}))
            return

        # Strip the ?json suffix the prpc clients use.
        bare = path.split("?", 1)[0]
        if bare.endswith("Tappd.Info") or bare.endswith("dstack.Info"):
            conn.sendall(_info_response())
        elif (bare.endswith("Tappd.TdxQuote")
              or bare.endswith("Tappd.GetQuote")
              or bare.endswith("dstack.GetQuote")):
            conn.sendall(_quote_response(body))
        else:
            conn.sendall(_make_response({"error": f"unknown_method:{bare}"}))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(sock_path: str) -> None:
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    os.chmod(sock_path, 0o666)
    s.listen(8)
    print(f"[mock-tappd] listening on {sock_path}", flush=True)

    stop = threading.Event()
    def _shutdown(*_):
        stop.set()
        try:
            s.close()
        except OSError:
            pass
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop.is_set():
            try:
                conn, _ = s.accept()
            except OSError:
                break
            t = threading.Thread(target=_handle, args=(conn,), daemon=True)
            t.start()
    finally:
        try:
            os.unlink(sock_path)
        except OSError:
            pass


if __name__ == "__main__":
    sock = sys.argv[1] if len(sys.argv) >= 2 else "/tmp/fish-tappd.sock"
    serve(sock)
