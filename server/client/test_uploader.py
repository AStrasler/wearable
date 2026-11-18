import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import uploader


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture
def start_http_server():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start

    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_load_frames_full_hex(tmp_path):
    p = tmp_path / "cap.jsonl"
    p.write_text(json.dumps({"type": "REALTIME_DATA", "seq": 2,
        "full_hex": "aa1800ff28020f3de10128663c00000000000000000001010d844e7c"}) + "\n")
    frames = uploader.load_frames(str(p))
    assert len(frames) == 1
    assert frames[0]["hex"].startswith("aa1800ff")


def test_load_frames_reframes_payload(tmp_path):
    # datalen + data_hex (bare payload) must be reframed into a full frame
    data = "00" * 30
    p = tmp_path / "m.jsonl"
    p.write_text(json.dumps({"wall": 1779570923.6, "cmd": 41, "datalen": 30, "data_hex": data}) + "\n")
    frames = uploader.load_frames(str(p))
    assert len(frames) == 1
    fr = bytes.fromhex(frames[0]["hex"])
    assert fr[0] == 0xAA and fr[4] == 43  # reframed as type-43


def test_derive_clock_ref_prefers_record_wall(tmp_path):
    p = tmp_path / "m.jsonl"
    # a record with a wall clock + a type-43 payload carrying device ts at data[4:8]
    # device ts bytes (LE) for 31538885 = c53ee101
    data = "7c953102" + "c53ee101" + "00" * 22
    p.write_text(json.dumps({"wall": 1779570923.0, "cmd": 41, "datalen": 30, "data_hex": data}) + "\n")
    ref = uploader.derive_clock_ref(str(p))
    assert ref["wall"] == 1779570923
    assert ref["device"] == 31538885


def test_chunk_batches():
    frames = [{"seq": i, "hex": "aa00"} for i in range(250)]
    batches = list(uploader.chunk(frames, size=100))
    assert [len(b) for b in batches] == [100, 100, 50]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/ingest",
    "//example.com/ingest",
    "http:///missing-host",
    "https://user:password@example.com/ingest",
    "https://example.com/ingest?token=secret",
    "https://example.com/ingest#fragment",
    "https://example.com/ingest?",
    "https://example.com/ingest#",
])
def test_post_batch_rejects_non_service_urls_before_opening(url, monkeypatch):
    def unexpected_build_opener(*_args, **_kwargs):
        raise AssertionError("unsupported URL reached the HTTP opener")

    monkeypatch.setattr(uploader.urllib.request, "build_opener", unexpected_build_opener)
    with pytest.raises(ValueError, match="ingest URL"):
        uploader.post_batch(url, "bearer-secret", {})


def test_validate_ingest_url_accepts_http_and_https():
    assert uploader._validate_ingest_url("http://localhost:8770/") == "http://localhost:8770"
    assert uploader._validate_ingest_url("https://ingest.example/v1") == "https://ingest.example/v1"


def test_post_batch_sends_to_configured_service(start_http_server):
    received = []

    class Handler(_QuietHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            response = json.dumps({"batch_id": "accepted"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = start_http_server(Handler)
    result = uploader.post_batch(f"http://127.0.0.1:{server.server_port}", "token", {"x": 1})

    assert result == {"batch_id": "accepted"}
    assert received == [("/v1/ingest", "Bearer token", {"x": 1})]


def test_post_batch_does_not_forward_token_to_redirect_target(start_http_server):
    redirected_requests = []

    class TargetHandler(_QuietHandler):
        def do_POST(self):
            redirected_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    target = start_http_server(TargetHandler)

    class RedirectHandler(_QuietHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/capture-token"
            )
            self.end_headers()

    source = start_http_server(RedirectHandler)
    with pytest.raises(HTTPError) as error:
        uploader.post_batch(
            f"http://127.0.0.1:{source.server_port}", "bearer-secret", {"x": 1}
        )
    assert error.value.code == 302
    assert redirected_requests == []
