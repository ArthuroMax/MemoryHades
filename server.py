#!/usr/bin/env python3
"""
Navigation-memory service for CoinFarm (reference implementation, Python standard library only).

  * Stores STRUCTURED, VALIDATED observations. Serves STRUCTURED advice. Never sends code, never executes anything it receives.
  * Authenticated (Bearer token; only a SHA-256 of each token is configured/stored), versioned (schema_version everywhere),
    rate limited, size limited. No IP addresses, no player ids, no account data are read or stored.
  * Learning = decayed counts per (map, kind, 4-stud cell): a state needs repeated evidence, fades with time, is weakened by
    contradicting observations and by a changed map signature. It is not "the last result wins".

Run:   NAV_TOKENS=secret1,secret2 NAV_DB_PATH=/var/data/nav.sqlite3 PORT=8080 python server.py
"""
import hashlib, hmac, json, math, os, re, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1          # what we write
MIN_SUPPORTED = 1           # advice ignores older rows (add a migration before raising this)
MAX_BODY, MAX_BATCH, MAX_ADVICE = 64 * 1024, 50, 500
HALF_LIFE = 14 * 86400
TTL = {"unreachable": 6 * 3600, "unreliable": 24 * 3600, "candidate": 7 * 86400, "invalid": 1800}
MIN_EVIDENCE = 2.5          # "three observations" once slow decay is allowed for (decayed counts never stay exactly >= 3)
KINDS = {"hop", "route", "stuck", "escape", "coin"}
OUTCOMES = {"success", "fail", "abandon", "unsafe"}
MAP_RE = re.compile(r"^[0-9A-Za-z_: \-]{1,64}$")
SIG_RE = re.compile(r"^[0-9A-Za-z_.:\-]{0,48}$")
TXT_RE = re.compile(r"^[A-Za-z0-9 _.\-:()]{0,32}$")

DDL_V1 = """
CREATE TABLE nav_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    map_id TEXT NOT NULL, kind TEXT NOT NULL, cell_x INTEGER NOT NULL, cell_z INTEGER NOT NULL,
    y REAL NOT NULL DEFAULT 0,
    successes REAL NOT NULL DEFAULT 0, failures REAL NOT NULL DEFAULT 0, abandons REAL NOT NULL DEFAULT 0, unsafe REAL NOT NULL DEFAULT 0,
    obs_count INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT NOT NULL DEFAULT '', map_sig TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, last_unsafe INTEGER,
    UNIQUE (map_id, kind, cell_x, cell_z)
);
CREATE TABLE record_sources (record_id INTEGER NOT NULL, source_hash TEXT NOT NULL, PRIMARY KEY (record_id, source_hash));
CREATE TABLE nav_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, schema_version INTEGER NOT NULL, received_at INTEGER NOT NULL, record_id INTEGER NOT NULL,
    outcome TEXT NOT NULL, reason TEXT NOT NULL, nav_state TEXT NOT NULL, recovery TEXT NOT NULL, escape_reached INTEGER NOT NULL,
    source_hash TEXT NOT NULL
);
CREATE INDEX idx_records_map ON nav_records (map_id);
"""
MIGRATIONS = [(1, DDL_V1)]   # (version, sql). Add (2, "ALTER TABLE ...") for the next schema; never edit an applied one.


def finite(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def state_of(s, f, a, u, last_seen, last_unsafe, now):
    """The same rules as the Lua client (Mem.stateOf). Counts are decayed to `now` first."""
    age = max(now - last_seen, 0)
    k = 0.5 ** (age / HALF_LIFE)
    s, f, a, u = s * k, f * k, a * k, u * k
    conf = (s + 1) / (s + f + 2)
    if u >= 0.5 and last_unsafe is not None and now - last_unsafe < TTL["invalid"]:
        return "invalid", conf, last_unsafe + TTL["invalid"]
    if a >= MIN_EVIDENCE and s < 0.5 and age < TTL["unreachable"]:
        return "unreachable", conf, last_seen + TTL["unreachable"]
    if f >= MIN_EVIDENCE and conf <= 0.3 and age < TTL["unreliable"]:
        return "unreliable", conf, last_seen + TTL["unreliable"]
    if s >= MIN_EVIDENCE and conf >= 0.75 and age < TTL["candidate"]:
        return "candidate", conf, last_seen + TTL["candidate"]
    return "unknown", conf, 0


class NavService:
    def __init__(self, db_path, token_hashes, clock=time.time, min_sources=1, rate_per_min=120):
        self.clock, self.min_sources, self.rate = clock, min_sources, rate_per_min
        self.hashes = [h.lower() for h in token_hashes]
        self.lock = threading.RLock()
        self.hits = {}
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try: self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError: pass
        self.migrate()

    # ---- schema versioning ----
    def migrate(self):
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
            done = {r[0] for r in self.db.execute("SELECT version FROM schema_migrations")}
            for ver, sql in MIGRATIONS:
                if ver not in done:
                    self.db.executescript(sql)
                    self.db.execute("INSERT INTO schema_migrations VALUES (?, ?)", (ver, int(self.clock())))
            self.db.commit()

    # ---- auth + rate limit ----
    def authenticate(self, header):
        if not header or not header.startswith("Bearer "): return 401, None
        digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        ok = False
        for h in self.hashes: ok |= hmac.compare_digest(digest, h)       # no early exit: constant-time over the list
        return (200, digest[:16]) if ok else (403, None)
    def rate_ok(self, src):
        now = self.clock(); w = [t for t in self.hits.get(src, []) if now - t < 60]
        if len(w) >= self.rate: self.hits[src] = w; return False
        w.append(now); self.hits[src] = w; return True

    # ---- ingest ----
    def _validate(self, o):
        if not isinstance(o, dict) or o.get("v") != SCHEMA_VERSION: return None
        mp, sig = o.get("map"), o.get("sig", "")
        if not (isinstance(mp, str) and MAP_RE.match(mp)) or not (isinstance(sig, str) and SIG_RE.match(sig)): return None
        if o.get("kind") not in KINDS or o.get("outcome") not in OUTCOMES: return None
        if not (finite(o.get("x"), -5000, 5000) and finite(o.get("z"), -5000, 5000) and finite(o.get("y", 0), -10000, 10000)): return None
        txt = {}
        for k in ("reason", "nav", "recovery"):
            v = o.get(k, "")
            if not (isinstance(v, str) and TXT_RE.match(v)): return None
            txt[k] = v
        esc = o.get("escape", False)
        if not isinstance(esc, bool): return None
        t = o.get("t", None)
        if t is not None and not finite(t, 0, 4e9): return None
        return dict(map=mp, sig=sig, kind=o["kind"], outcome=o["outcome"], x=o["x"], y=o.get("y", 0), z=o["z"], esc=esc, t=t, **txt)

    def ingest(self, src, payload):
        if not isinstance(payload, dict): return 400, {"error": "body must be an object"}
        if payload.get("schema_version") != SCHEMA_VERSION:
            return 422, {"error": "unsupported schema_version", "supported": [SCHEMA_VERSION]}
        obs = payload.get("observations")
        if not isinstance(obs, list) or not (1 <= len(obs) <= MAX_BATCH): return 422, {"error": f"observations must hold 1..{MAX_BATCH} items"}
        clean = [self._validate(o) for o in obs]
        if any(c is None for c in clean): return 422, {"error": "an observation failed validation"}
        now = int(self.clock())
        with self.lock:
            for c in clean:
                self._apply(src, c, now)
            self.db.commit()
        return 200, {"schema_version": SCHEMA_VERSION, "accepted": len(clean)}

    def _apply(self, src, c, now):
        cx, cz = math.floor(c["x"] / 4), math.floor(c["z"] / 4)
        ts = now if c["t"] is None else int(min(max(c["t"], now - 7 * 86400), now))
        row = self.db.execute("SELECT * FROM nav_records WHERE map_id=? AND kind=? AND cell_x=? AND cell_z=?", (c["map"], c["kind"], cx, cz)).fetchone()
        if row is None:
            cur = self.db.execute("INSERT INTO nav_records (schema_version,map_id,kind,cell_x,cell_z,y,first_seen,last_seen,map_sig) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (SCHEMA_VERSION, c["map"], c["kind"], cx, cz, c["y"], ts, ts, c["sig"]))
            rid, s, f, a, u, last_seen, last_unsafe, sig = cur.lastrowid, 0.0, 0.0, 0.0, 0.0, ts, None, c["sig"]
        else:
            rid, s, f, a, u = row["id"], row["successes"], row["failures"], row["abandons"], row["unsafe"]
            last_seen, last_unsafe, sig = row["last_seen"], row["last_unsafe"], row["map_sig"]
            k = 0.5 ** (max(ts - last_seen, 0) / HALF_LIFE)
            s, f, a, u = s * k, f * k, a * k, u * k
            if c["sig"] and sig and c["sig"] != sig: s, f, a, u = s * .25, f * .25, a * .25, u * .25   # the map is not what we remembered
            sig = c["sig"] or sig
        o = c["outcome"]
        if o == "success" and f + a >= 3 * (s + 1): f, a = f * .5, a * .5                                # contradiction: weaken the old verdict
        if o in ("fail", "abandon") and s >= 3 * (f + a + 1): s *= .5
        if o == "success": s += 1
        elif o == "fail": f += 1
        elif o == "abandon": a += 1
        else: u, s, last_unsafe = u + 1, s * .3, ts
        self.db.execute("UPDATE nav_records SET successes=?,failures=?,abandons=?,unsafe=?,obs_count=obs_count+1,last_reason=?,map_sig=?,last_seen=?,last_unsafe=?,y=?,schema_version=? WHERE id=?",
                        (s, f, a, u, c["reason"], sig, max(ts, last_seen), last_unsafe, c["y"], SCHEMA_VERSION, rid))
        self.db.execute("INSERT OR IGNORE INTO record_sources VALUES (?,?)", (rid, src))
        self.db.execute("INSERT INTO nav_observations (schema_version,received_at,record_id,outcome,reason,nav_state,recovery,escape_reached,source_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                        (SCHEMA_VERSION, now, rid, o, c["reason"], c["nav"], c["recovery"], 1 if c["esc"] else 0, src))
        self.db.execute("DELETE FROM nav_observations WHERE id <= (SELECT MAX(id) FROM nav_observations) - 20000")   # bounded raw log

    # ---- advice ----
    def advice(self, map_id):
        if not MAP_RE.match(map_id or ""): return 422, {"error": "bad map id"}
        now = self.clock(); out = []
        with self.lock:
            rows = self.db.execute("SELECT r.*, (SELECT COUNT(*) FROM record_sources s WHERE s.record_id=r.id) AS nsrc FROM nav_records r "
                                   "WHERE map_id=? AND schema_version>=?", (map_id, MIN_SUPPORTED)).fetchall()
        for r in rows:
            st, conf, exp = state_of(r["successes"], r["failures"], r["abandons"], r["unsafe"], r["last_seen"], r["last_unsafe"], now)
            if st == "unknown" or r["nsrc"] < self.min_sources: continue
            out.append({"kind": r["kind"], "x": r["cell_x"] * 4 + 2, "y": round(r["y"], 1), "z": r["cell_z"] * 4 + 2, "state": st,
                        "confidence": round(conf, 3), "observations": r["obs_count"], "sources": r["nsrc"], "last_seen": r["last_seen"], "expires": int(exp)})
        out.sort(key=lambda d: -d["observations"])
        return 200, {"schema_version": SCHEMA_VERSION, "server_time": int(now), "map": map_id, "records": out[:MAX_ADVICE]}

    def health(self):
        try:
            with self.lock: self.db.execute("SELECT 1").fetchone()
            return 200, {"ok": True, "schema": SCHEMA_VERSION, "db": "ok"}
        except Exception:
            return 503, {"ok": False, "db": "error"}


def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass                      # no request logging: no addresses are kept anywhere
        def _send(self, code, body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _auth(self):
            code, src = svc.authenticate(self.headers.get("Authorization"))
            if code != 200: self._send(code, {"error": "unauthorized" if code == 401 else "forbidden"}); return None
            if not svc.rate_ok(src): self._send(429, {"error": "rate limited"}); return None
            return src
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz": return self._send(*svc.health())
            m = re.fullmatch(r"/v1/maps/([^/]+)/advice", path)
            if not m: return self._send(404, {"error": "not found"})
            if self._auth() is None: return
            self._send(*svc.advice(unquote(m.group(1))))
        def do_POST(self):
            if urlparse(self.path).path != "/v1/observations": return self._send(404, {"error": "not found"})
            src = self._auth()
            if src is None: return
            try: n = int(self.headers.get("Content-Length", ""))
            except ValueError: return self._send(411, {"error": "length required"})
            if n > MAX_BODY: return self._send(413, {"error": "body too large"})
            try: payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception: return self._send(400, {"error": "malformed json"})
            self._send(*svc.ingest(src, payload))
    return H


def serve(svc, host="0.0.0.0", port=8080):
    srv = ThreadingHTTPServer((host, port), make_handler(svc)); srv.daemon_threads = True; return srv


if __name__ == "__main__":
    tokens = [t for t in os.environ.get("NAV_TOKENS", "").split(",") if t]
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in tokens] + [h for h in os.environ.get("NAV_TOKEN_HASHES", "").split(",") if h]
    if not hashes: sys.exit("refusing to start without NAV_TOKENS / NAV_TOKEN_HASHES (the service is never open)")
    svc = NavService(os.environ.get("NAV_DB_PATH", "navmem.sqlite3"), hashes, min_sources=int(os.environ.get("NAV_MIN_SOURCES", "1")))
    port = int(os.environ.get("PORT", "8080")); print(f"nav-memory listening on :{port}, db={os.environ.get('NAV_DB_PATH', 'navmem.sqlite3')}")
    serve(svc, port=port).serve_forever()
