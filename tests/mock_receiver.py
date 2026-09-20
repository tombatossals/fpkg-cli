"""Mock of pkg-receiver.elf for sender-side testing.

Covers the file endpoints (stat/mkdir/write/done) used by the copy path
and, for the install path, /api, /api/version, /api/status and
/api/install: a pushed URL is really downloaded with Range requests the
way the payload does it, then the mock reports busy for a moment so the
sender's "busy -> idle = installed" gate can be exercised.

    python3 tests/mock_receiver.py [port] [bind-address]
"""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE = {}  # path -> bytearray
STATE = {"active": 0, "installed": []}
STATE_LOCK = threading.Lock()

# How long a "download" is held as an install after the bytes arrive.
INSTALL_SECONDS = 2.0
CHUNK = 1024 * 1024


def fetch_install(url):
    """Pull the package the way the payload does: HEAD, then ranged GETs."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while got < total:
            end = min(total - 1, got + CHUNK - 1)
            req = urllib.request.Request(url)
            req.add_header("Range", "bytes=%d-%d" % (got, end))
            with urllib.request.urlopen(req, timeout=60) as r:
                buf = r.read()
            if not buf:
                break
            got += len(buf)
        sys.stdout.write("MOCK downloaded %d/%d bytes from %s\n" % (got, total, url))
        sys.stdout.flush()
        time.sleep(INSTALL_SECONDS)
        with STATE_LOCK:
            STATE["installed"].append({"url": url, "bytes": got})
    except Exception as ex:
        sys.stdout.write("MOCK install failed: %s\n" % ex)
        sys.stdout.flush()
    finally:
        with STATE_LOCK:
            STATE["active"] -= 1


class H(BaseHTTPRequestHandler):
    server_version = "MockDPI/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet: the explicit self.log() lines below are the signal

    def log(self, *a):
        sys.stdout.write("MOCK %s\n" % (" ".join(str(x) for x in a)))
        sys.stdout.flush()

    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_text(self, text):
        """The payload answers some failures as 200 + plain `error:...`."""
        b = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, q = self._q()
        self.log("GET", self.path)
        if path == "/api":
            self._send(200, {"status": "fail",
                             "error": "Unsupported method: use POST /api/install"})
        elif path == "/api/version":
            self._send(200, {"build": "mock"})
        elif path == "/api/status":
            with STATE_LOCK:
                active = STATE["active"]
            self._send(200, {"busy": active > 0, "active": active,
                             "pull": False, "pullName": "",
                             "pullGot": 0, "pullWant": -1, "pullPaused": False})
        elif path == "/api/files/stat":
            p = q.get("path", "")
            if p in STORE:
                self._send(200, {"exists": True, "size": len(STORE[p])})
            else:
                self._send(200, {"exists": False, "size": 0})
        else:
            self._send(404, {"error": "unknown"})

    def do_POST(self):
        path, q = self._q()
        body = self._body()
        self.log("POST", self.path, "body_len=%d" % len(body))
        if path == "/api/install":
            try:
                j = json.loads(body.decode())
            except Exception:
                self._send(200, {"status": "fail", "error": "bad json"})
                return
            pkgs = j.get("packages") or []
            if not pkgs:
                self._send(200, {"status": "fail", "error": "no package url"})
                return
            url = urllib.parse.unquote(pkgs[0])
            with STATE_LOCK:
                STATE["active"] += 1
            threading.Thread(target=fetch_install, args=(url,), daemon=True).start()
            self._send(200, {"status": "success"})
        elif path == "/api/files/mkdir":
            self._send(200, {"ok": True})
        elif path == "/api/files/write":
            p = q.get("path", "")
            off = int(q.get("offset", "0"))
            cur = STORE.get(p, bytearray())
            if len(cur) < off:
                cur.extend(b"\x00" * (off - len(cur)))
            cur[off:off + len(body)] = body
            STORE[p] = cur
            self._send(200, {"ok": True})
        elif path == "/api/files/done":
            try:
                j = json.loads(body.decode())
            except Exception:
                self._send_text("error:bad json")
                return
            p, want = j.get("path", ""), int(j.get("size", -1))
            if p in STORE and len(STORE[p]) == want:
                self._send(200, {"ok": True, "size": want})
            else:
                self._send_text("error:size mismatch")
        else:
            self._send(404, {"error": "unknown"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 12800
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), H)
    print("mock on %s:%d" % (host, port), flush=True)
    srv.serve_forever()
