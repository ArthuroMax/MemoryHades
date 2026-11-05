"""SIMULATED Pastefy: a local stand-in implementing GET/PUT /api/v2/paste/{id} (shapes taken from docs.pastefy.app).
It is NOT the real service: it exists so the failure paths (429, 5xx, outage, junk bodies) can be exercised."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakePastefy:
    def __init__(self, token="PT", paste_id="abc123", content="", wrapped=True, visibility="PRIVATE"):
        self.token, self.paste_id, self.content, self.wrapped, self.visibility = token, paste_id, content, wrapped, visibility
        self.mode = "ok"          # ok | 500 | 429 | drop | html | nocontent | badjson | redirect | put500 | putdrop
        self.counts = {"GET": 0, "PUT": 0}
        self.headers_seen = []
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _out(self, code, body=b"", ctype="application/json", extra=None):
                self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)
            def _gate(self, method):
                with fake.lock:
                    fake.counts[method] += 1; fake.headers_seen.append(dict(self.headers))
                if self.headers.get("Authorization") != "Bearer " + fake.token: self._out(401, b'{"error":"bad token"}'); return False
                if self.path != "/api/v2/paste/" + fake.paste_id: self._out(404, b'{"error":"nf"}'); return False
                m = fake.mode
                if m == "drop" or (m == "putdrop" and method == "PUT"): self.connection.close(); return False
                if m == "500" or (m == "put500" and method == "PUT"): self._out(500, b"boom"); return False
                if m == "429": self._out(429, b"slow down", extra={"Retry-After": "120"}); return False
                if m == "redirect": self._out(302, b"", extra={"Location": "http://127.0.0.1:1/steal"}); return False
                return True
            def do_GET(self):
                if not self._gate("GET"): return
                if fake.mode == "html": return self._out(200, b"<html>cdn error</html>", "text/html")
                if fake.mode == "badjson": return self._out(200, b'{"paste": {"content": "x"')
                if fake.mode == "nocontent": return self._out(200, b'{"paste":{"id":"abc123"}}')
                p = {"id": fake.paste_id, "content": fake.content, "title": "memory", "encrypted": False, "visibility": fake.visibility}
                self._out(200, json.dumps({"success": True, "paste": p} if fake.wrapped else p).encode())
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(n)
                if not self._gate("PUT"): return
                fake.content = json.loads(body)["content"]
                self._out(204)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base = "http://127.0.0.1:%d/api/v2" % self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self): self.srv.shutdown(); self.srv.server_close()
