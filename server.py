#!/usr/bin/env python3
"""
Navigation-memory service for CoinFarm, v2: Pastefy JSON storage (Python standard library only).

  * The ONE persistent store is a single designated Pastefy paste holding a versioned JSON document. No SQLite, no disk.
  * The Pastefy API token lives only in this service's environment (PASTEFY_API_TOKEN). It is never sent to a client,
    never logged, never put in a response, and is not sent anywhere a redirect could take it.
  * The in-memory copy of the document is authoritative while the process runs. Clients are served from it, so Pastefy
    is not queried per request (cache). Writes are batched (PASTEFY_FLUSH_SECONDS) into updates of the SAME paste.
  * Safety rules: nothing is written until the document has been loaded; a corrupted / empty / foreign / newer-version
    response never replaces valid memory; a failed write keeps the data dirty and is retried with back-off.
  * Clients (the Lua script) authenticate with their own Bearer token (only SHA-256 hashes are configured) and stay
    fully functional with local memory when this service is down.

Run:  NAV_TOKENS=secret1 PASTEFY_API_TOKEN=... PASTEFY_PASTE_ID=... PORT=8080 python server.py
"""
import copy, hashlib, hmac, http.client, json, math, os, re, signal, socket, sys, threading, time
import urllib.error, urllib.request
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1          # client <-> service wire schema
DOC_FORMAT = "coinfarm-navmemory"
DOC_VERSION = 1             # schema of the JSON stored in Pastefy. Add a migration to MIGRATIONS before raising it.
MIN_SUPPORTED = 1
MAX_BODY, MAX_BATCH, MAX_ADVICE = 64 * 1024, 50, 500
MAX_MAPS, MAX_CELLS_PER_MAP, MAX_SRC, MAX_BATCH_IDS = 300, 4000, 8, 64
MAX_REMOTE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_DOC_BYTES = 600_000   # Pastefy documents no size limit; stay conservative and prune
HALF_LIFE = 14 * 86400
TTL = {"unreachable": 6 * 3600, "unreliable": 24 * 3600, "candidate": 7 * 86400, "invalid": 1800}
MIN_EVIDENCE = 2.5
KINDS = {"hop", "route", "stuck", "escape", "coin"}
OUTCOMES = {"success", "fail", "abandon", "unsafe"}
MAP_RE = re.compile(r"^[0-9A-Za-z_: \-]{1,64}$")
SIG_RE = re.compile(r"^[0-9A-Za-z_.:\-]{0,48}$")
TXT_RE = re.compile(r"^[A-Za-z0-9 _.\-:()]{0,32}$")
KEY_RE = re.compile(r"^(hop|route|stuck|escape|coin)\|(-?\d{1,4})\|(-?\d{1,4})$")
BATCH_RE = re.compile(r"^[0-9A-Za-z_\-]{8,40}$")
PASTE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SRC_RE = re.compile(r"^[0-9a-f]{8}$")


def log(msg):
    print(time.strftime("%H:%M:%S", time.gmtime()), msg, flush=True)   # never pass secrets in here


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


# ------------------------------------------------------------------------------------------------------------------
# the JSON document stored in Pastefy
#   {"format":"coinfarm-navmemory","doc_version":1,"memory_id":"..","revision":N,"updated_at":T,
#    "maps":{"<map id>":{"sig":"..","ts":T,"cells":{"<kind>|<cx>|<cz>":{"s","f","a","u","n","t0","t1","y","lu"?,"why"?,"src"?}}}},
#    "recent_batches":["..."]}
# kinds: hop (jump/platform hops), route, stuck, escape (learned escape destinations), coin (coin locations)
# ------------------------------------------------------------------------------------------------------------------
def new_doc(now):
    return {"format": DOC_FORMAT, "doc_version": DOC_VERSION, "memory_id": hashlib.sha256(os.urandom(16)).hexdigest()[:16],
            "revision": 0, "updated_at": int(now), "maps": {}, "recent_batches": []}


MIGRATIONS = {}   # {from_version: fn(doc) -> doc}; empty while DOC_VERSION == 1


def _clean_cell(key, c):
    if not KEY_RE.match(key) or not isinstance(c, dict):
        return None
    out = {}
    for k in ("s", "f", "a", "u"):
        if not finite(c.get(k), 0, 1e6): return None
        out[k] = float(c[k])
    if not finite(c.get("n"), 0, 1e7) or not finite(c.get("t0"), 0, 4e9) or not finite(c.get("t1"), 0, 4e9): return None
    out["n"], out["t0"], out["t1"] = int(c["n"]), c["t0"], c["t1"]
    out["y"] = c["y"] if finite(c.get("y", 0), -1e4, 1e4) else 0
    if c.get("lu") is not None:
        if not finite(c["lu"], 0, 4e9): return None
        out["lu"] = c["lu"]
    why = c.get("why", "")
    if isinstance(why, str) and TXT_RE.match(why) and why: out["why"] = why
    src = c.get("src", [])
    out["src"] = [x for x in src if isinstance(x, str) and SRC_RE.match(x)][:MAX_SRC] if isinstance(src, list) else []
    return out


def parse_doc(text):
    """-> (kind, doc, info). kind: ok | empty | corrupt | foreign | future. Never raises. `doc` only for kind == ok."""
    if not isinstance(text, str) or text.strip() in ("", "{}", "null"):
        return "empty", None, {}
    if len(text) > MAX_REMOTE_BYTES:
        return "corrupt", None, {"why": "too large"}
    try:
        d = json.loads(text)
    except (ValueError, RecursionError):
        return "corrupt", None, {"why": "not json"}
    if not isinstance(d, dict):
        return "foreign", None, {}
    if d.get("format") != DOC_FORMAT:
        return "foreign", None, {}
    ver = d.get("doc_version")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        return "corrupt", None, {"why": "bad version"}
    if ver > DOC_VERSION:
        return "future", None, {"version": ver}
    while ver < DOC_VERSION:
        d = MIGRATIONS[ver](d); ver += 1
    maps_in = d.get("maps")
    if not isinstance(maps_in, dict) or not finite(d.get("revision"), 0, 1e12):
        return "corrupt", None, {"why": "shape"}
    mid = d.get("memory_id")
    if not (isinstance(mid, str) and re.match(r"^[0-9A-Za-z]{8,32}$", mid)):
        return "corrupt", None, {"why": "memory_id"}
    total = bad = 0
    maps = {}
    for mp, m in maps_in.items():
        if not (isinstance(mp, str) and MAP_RE.match(mp) and isinstance(m, dict) and isinstance(m.get("cells"), dict)):
            total += 1; bad += 1; continue
        sig = m.get("sig", "")
        cm = {"sig": sig if isinstance(sig, str) and SIG_RE.match(sig) else "", "ts": m["ts"] if finite(m.get("ts"), 0, 4e9) else 0, "cells": {}}
        for key, c in m["cells"].items():
            total += 1
            cc = _clean_cell(key, c)
            if cc is None: bad += 1
            else: cm["cells"][key] = cc
        maps[mp] = cm
    if total and bad / total > 0.10:       # a document that is mostly junk is corrupt, not "a bit dirty"
        return "corrupt", None, {"why": "too many invalid entries", "bad": bad, "total": total}
    rb = d.get("recent_batches", [])
    rb = [x for x in rb if isinstance(x, str) and BATCH_RE.match(x)][-MAX_BATCH_IDS:] if isinstance(rb, list) else []
    doc = {"format": DOC_FORMAT, "doc_version": DOC_VERSION, "memory_id": mid, "revision": int(d["revision"]),
           "updated_at": int(d["updated_at"]) if finite(d.get("updated_at"), 0, 4e9) else 0, "maps": maps, "recent_batches": rb}
    return "ok", doc, {"bad": bad, "total": total}


def count_cells(doc):
    return sum(len(m["cells"]) for m in doc["maps"].values())


def merge_docs(local, remote):
    """Fold `remote` into `local` (in place): per cell the record seen most recently wins (ties: more observations);
    contributor lists are unioned. Used when another instance wrote the paste, or after a restart."""
    for mp, rm in remote["maps"].items():
        lm = local["maps"].get(mp)
        if lm is None:
            local["maps"][mp] = copy.deepcopy(rm); continue
        if rm["ts"] > lm["ts"]:
            lm["sig"], lm["ts"] = rm["sig"], rm["ts"]
        for key, rc in rm["cells"].items():
            lc = lm["cells"].get(key)
            if lc is None:
                lm["cells"][key] = copy.deepcopy(rc)
            else:
                src = list(dict.fromkeys(lc.get("src", []) + rc.get("src", [])))[:MAX_SRC]
                if (rc["t1"], rc["n"]) > (lc["t1"], lc["n"]):
                    lm["cells"][key] = copy.deepcopy(rc)
                    lc = lm["cells"][key]
                lc["src"] = src
    seen = list(dict.fromkeys(local["recent_batches"] + remote["recent_batches"]))
    local["recent_batches"] = seen[-MAX_BATCH_IDS:]
    local["revision"] = max(local["revision"], remote["revision"])


def prune_doc(doc, now, max_bytes):
    """Drop cells whose decayed evidence is negligible; then enforce the per-map and byte caps (oldest evidence first)."""
    def value(c):
        return (c["s"] + c["f"] + c["a"] + c["u"]) * 0.5 ** (max(now - c["t1"], 0) / HALF_LIFE)
    dropped = 0
    for mp in list(doc["maps"]):
        cells = doc["maps"][mp]["cells"]
        for key in [k for k, c in cells.items() if now - c["t1"] > 30 * 86400 and value(c) < 0.05]:
            del cells[key]; dropped += 1
        if len(cells) > MAX_CELLS_PER_MAP:
            for key in sorted(cells, key=lambda k: value(cells[k]))[: len(cells) - MAX_CELLS_PER_MAP]:
                del cells[key]; dropped += 1
        if not cells and now - doc["maps"][mp]["ts"] > 30 * 86400:
            del doc["maps"][mp]
    if len(doc["maps"]) > MAX_MAPS:
        for mp in sorted(doc["maps"], key=lambda m: doc["maps"][m]["ts"])[: len(doc["maps"]) - MAX_MAPS]:
            dropped += count_cells({"maps": {mp: doc["maps"][mp]}}); del doc["maps"][mp]
    while len(dumps(doc)) > max_bytes:
        allc = [(c["t1"], mp, k) for mp, m in doc["maps"].items() for k, c in m["cells"].items()]
        if not allc: break
        allc.sort()
        for _, mp, k in allc[: max(len(allc) // 10, 1)]:
            del doc["maps"][mp]["cells"][k]; dropped += 1
    return dropped


def dumps(doc):
    return json.dumps(doc, separators=(",", ":"), sort_keys=True)


# ------------------------------------------------------------------------------------------------------------------
# Pastefy transport
# ------------------------------------------------------------------------------------------------------------------
FetchResult = namedtuple("FetchResult", "status text retry_after meta")      # status: ok | auth | not_found | rate_limited | unavailable | bad_response | encrypted
WriteResult = namedtuple("WriteResult", "status retry_after")                # status: ok | auth | not_found | rate_limited | unavailable | bad_response | too_large


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):    # a redirect could carry the Authorization header to another host
        return None


def _retry_after(headers):
    try:
        v = float(headers.get("Retry-After", ""))
        return min(max(v, 0), 3600)
    except (TypeError, ValueError):
        return None


class PastefyStore:
    UA = "coinfarm-nav-memory/2"

    def __init__(self, token, paste_id, base="https://pastefy.app/api/v2", timeout=10):
        if not token or not isinstance(token, str):
            raise ValueError("PASTEFY_API_TOKEN is empty")
        if not PASTE_ID_RE.match(paste_id or ""):
            raise ValueError("PASTEFY_PASTE_ID is missing or malformed")
        u = urlparse(base)
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
            raise ValueError("PASTEFY_API_BASE must be https")
        self.token, self.paste_id, self.base, self.timeout = token, paste_id, base.rstrip("/"), timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _call(self, method, payload=None):
        url = "%s/paste/%s" % (self.base, self.paste_id)
        data = json.dumps(payload).encode() if payload is not None else None
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/json", "User-Agent": self.UA}
        if data is not None: h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.headers, r.read(MAX_REMOTE_BYTES + 1)
        except urllib.error.HTTPError as e:
            try: body = e.read(2048)
            except Exception: body = b""
            return e.code, e.headers, body
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, http.client.HTTPException):
            return None, None, b""

    @staticmethod
    def _classify(code, headers):
        if code is None or code >= 500: return "unavailable"
        if code in (401, 403): return "auth"
        if code == 404: return "not_found"
        if code == 429: return "rate_limited"
        if code == 413: return "too_large"
        return "bad_response"

    def fetch(self):
        code, headers, body = self._call("GET")
        if code != 200:
            return FetchResult(self._classify(code, headers), None, _retry_after(headers) if headers else None, {})
        if len(body) > MAX_REMOTE_BYTES:
            return FetchResult("bad_response", None, None, {})
        try:
            d = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return FetchResult("bad_response", None, None, {})
        obj = d.get("paste") if isinstance(d, dict) and isinstance(d.get("paste"), dict) else d   # tolerate wrapped and flat shapes
        if not isinstance(obj, dict) or not isinstance(obj.get("content"), str):
            return FetchResult("bad_response", None, None, {})        # a missing field is NOT an empty paste
        if obj.get("encrypted") is True:
            return FetchResult("encrypted", None, None, {})
        return FetchResult("ok", obj["content"], None, {"visibility": str(obj.get("visibility", ""))})

    def write(self, text):
        code, headers, _ = self._call("PUT", {"content": text})
        if code in (200, 204):
            return WriteResult("ok", None)
        return WriteResult(self._classify(code, headers), _retry_after(headers) if headers else None)


# ------------------------------------------------------------------------------------------------------------------
# the service
# ------------------------------------------------------------------------------------------------------------------
class NavService:
    def __init__(self, store, token_hashes, clock=time.time, min_sources=1, rate_per_min=120, flush_interval=90, cache_ttl=300,
                 max_bytes=DEFAULT_MAX_DOC_BYTES, adopt_foreign=False, confirm_empty=2, empty_delay=5, bad_streak_limit=3):
        self.store, self.clock, self.min_sources, self.rate = store, clock, min_sources, rate_per_min
        self.flush_interval, self.cache_ttl, self.max_bytes = flush_interval, cache_ttl, max_bytes
        self.adopt_foreign, self.confirm_empty, self.empty_delay, self.bad_limit = adopt_foreign, confirm_empty, empty_delay, bad_streak_limit
        self.hashes = [h.lower() for h in token_hashes]
        self.lock = threading.RLock()       # guards doc / counters (never held across network I/O)
        self._io = threading.Lock()         # at most one Pastefy operation at a time
        self.hits = {}
        self.doc = new_doc(clock()) if store is None else None
        self.loaded = store is None
        self.mode = "ok"                    # ok | blocked (foreign/corrupt paste) | readonly (newer doc_version)
        self.dirty, self.seq, self.dirty_obs = False, 0, 0
        self.last_flush, self.last_fetch, self.want_refresh = -1e18, -1e18, False
        self.empty_streak = self.bad_streak = 0
        self.init_empty = False             # we initialised over a paste that was confirmed empty: the first write may replace it
        self.warnings = set()
        self.sync = {"fails": 0, "next_at": 0.0, "last_ok": None, "last_error": None, "requests": 0, "writes": 0, "verify_fail": 0}
        self._stop = threading.Event()
        self._thread = None

    # ---- auth + rate limit ----
    def authenticate(self, header):
        if not header or not header.startswith("Bearer "): return 401, None
        digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        ok = False
        for h in self.hashes: ok |= hmac.compare_digest(digest, h)
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
        if not isinstance(obs, list) or not (1 <= len(obs) <= MAX_BATCH): return 422, {"error": "observations must hold 1..%d items" % MAX_BATCH}
        bid = payload.get("batch_id")
        if bid is not None and not (isinstance(bid, str) and BATCH_RE.match(bid)): return 422, {"error": "bad batch_id"}
        clean = [self._validate(o) for o in obs]
        if any(c is None for c in clean): return 422, {"error": "an observation failed validation"}
        now = int(self.clock())
        with self.lock:
            if not self.loaded or self.mode != "ok":
                return 503, {"error": "memory not available", "retry": True}     # the client keeps its queue and retries
            if bid and bid in self.doc["recent_batches"]:
                return 200, {"schema_version": SCHEMA_VERSION, "accepted": len(clean), "duplicate": True}   # idempotent retry
            for c in clean:
                self._apply(src, c, now)
            if bid:
                self.doc["recent_batches"] = (self.doc["recent_batches"] + [bid])[-MAX_BATCH_IDS:]
            self.dirty, self.dirty_obs = True, self.dirty_obs + len(clean)
            self.seq += 1
        return 200, {"schema_version": SCHEMA_VERSION, "accepted": len(clean)}

    def _apply(self, src, c, now):
        mp = self.doc["maps"].get(c["map"])
        if mp is None:
            if len(self.doc["maps"]) >= MAX_MAPS:
                return
            mp = self.doc["maps"][c["map"]] = {"sig": c["sig"], "ts": now, "cells": {}}
        cx, cz = math.floor(c["x"] / 4), math.floor(c["z"] / 4)
        ts = now if c["t"] is None else int(min(max(c["t"], now - 7 * 86400), now))
        if c["sig"] and mp["sig"] and c["sig"] != mp["sig"]:
            for r in mp["cells"].values():       # the map is not what we remembered: everything is weakened to a quarter
                for k in ("s", "f", "a", "u"): r[k] *= .25
        mp["sig"] = c["sig"] or mp["sig"]
        mp["ts"] = max(mp["ts"], ts)
        key = "%s|%d|%d" % (c["kind"], cx, cz)
        r = mp["cells"].get(key)
        if r is None:
            if len(mp["cells"]) >= MAX_CELLS_PER_MAP: return
            r = mp["cells"][key] = {"s": 0.0, "f": 0.0, "a": 0.0, "u": 0.0, "n": 0, "t0": ts, "t1": ts, "y": c["y"], "src": []}
        k = 0.5 ** (max(ts - r["t1"], 0) / HALF_LIFE)
        s, f, a, u = r["s"] * k, r["f"] * k, r["a"] * k, r["u"] * k
        o = c["outcome"]
        if o == "success" and f + a >= 3 * (s + 1): f, a = f * .5, a * .5                      # contradiction: weaken the old verdict
        if o in ("fail", "abandon") and s >= 3 * (f + a + 1): s *= .5
        if o == "success": s += 1
        elif o == "fail": f += 1
        elif o == "abandon": a += 1
        else:
            u, s = u + 1, s * .3
            r["lu"] = ts
        r.update(s=s, f=f, a=a, u=u, n=r["n"] + 1, t1=max(ts, r["t1"]), y=c["y"])
        if c["reason"]: r["why"] = c["reason"]
        h = src[:8]
        if h not in r["src"] and len(r["src"]) < MAX_SRC: r["src"].append(h)

    # ---- advice (served from the in-memory copy: no Pastefy request per call) ----
    def advice(self, map_id):
        if not MAP_RE.match(map_id or ""): return 422, {"error": "bad map id"}
        now = self.clock(); out = []
        with self.lock:
            self.want_refresh = True
            if not self.loaded or self.mode != "ok":
                return 503, {"error": "memory not available", "retry": True}
            m = self.doc["maps"].get(map_id)
            cells = list(m["cells"].items()) if m else []
            rev, mid = self.doc["revision"], self.doc["memory_id"]
        for key, r in cells:
            st, conf, exp = state_of(r["s"], r["f"], r["a"], r["u"], r["t1"], r.get("lu"), now)
            nsrc = len(r.get("src", []))
            if st == "unknown" or nsrc < self.min_sources: continue
            kind, cx, cz = KEY_RE.match(key).groups()
            out.append({"kind": kind, "x": int(cx) * 4 + 2, "y": round(r["y"], 1), "z": int(cz) * 4 + 2, "state": st,
                        "confidence": round(conf, 3), "observations": r["n"], "sources": nsrc, "last_seen": int(r["t1"]), "expires": int(exp)})
        out.sort(key=lambda d: -d["observations"])
        return 200, {"schema_version": SCHEMA_VERSION, "server_time": int(now), "map": map_id, "revision": rev, "memory_id": mid,
                     "records": out[:MAX_ADVICE]}

    # ---- health: 200 while the PROCESS works, even when Pastefy is down (a failing health check makes Render restart us) ----
    def storage_state(self):
        if self.store is None: return "volatile"
        if self.mode == "readonly": return "readonly"
        if self.mode == "blocked": return "blocked"
        if not self.loaded: return "loading"
        if self.sync["fails"] > 0: return "degraded"
        return "pending" if self.dirty else "synced"

    def health(self):
        with self.lock:
            st = {"provider": "volatile" if self.store is None else "pastefy", "state": self.storage_state(), "loaded": self.loaded,
                  "dirty": self.dirty, "revision": self.doc["revision"] if self.doc else None, "fails": self.sync["fails"],
                  "last_ok": self.sync["last_ok"], "last_error": self.sync["last_error"], "warnings": sorted(self.warnings)}
        return 200, {"ok": True, "schema": SCHEMA_VERSION, "storage": st}

    # ---- Pastefy synchronisation ----
    def _fail(self, code, retry_after, now):
        s = self.sync
        s["fails"] += 1; s["last_error"] = code
        delay = min(30 * 2 ** (s["fails"] - 1), 900)
        if code == "rate_limited": delay = max(delay, retry_after or 0)
        if code in ("auth", "not_found", "encrypted"): delay = max(delay, 900)
        s["next_at"] = now + delay
        if s["fails"] in (1, 5, 20): log("pastefy sync problem: %s (attempt %d, retry in %ds)" % (code, s["fails"], delay))

    def _ok(self, now):
        s = self.sync
        if s["fails"]: log("pastefy sync recovered")
        s["fails"], s["next_at"], s["last_error"], s["last_ok"] = 0, 0.0, None, now

    def _fetch(self, now):
        self.sync["requests"] += 1
        res = self.store.fetch()
        if res.status != "ok":
            self._fail(res.status, res.retry_after, now)
            return None
        if "public" in res.meta.get("visibility", "").lower():
            self.warnings.add("paste_is_public")
        self.last_fetch = now
        return parse_doc(res.text)

    def _init_fresh(self, now, why):
        with self.lock:
            self.doc = new_doc(now)
            self.loaded, self.mode, self.dirty = True, "ok", True
            self.init_empty = True
            self.seq += 1
        log("memory initialised (%s)" % why)

    def _load(self, now):
        out = self._fetch(now)
        if out is None: return "fail:" + self.sync["last_error"]
        kind, doc, info = out
        if kind == "ok":
            with self.lock:
                self.doc, self.loaded, self.mode = doc, True, "ok"
            self.empty_streak = 0; self.last_flush = now; self._ok(now); log("memory loaded (revision %d, %d cells)" % (doc["revision"], count_cells(doc)))
            return "loaded"
        if kind == "empty":
            self.empty_streak += 1
            if self.empty_streak < self.confirm_empty:       # one blank answer could be a glitch over real data: ask again first
                self.sync["next_at"] = now + self.empty_delay
                return "confirming-empty"
            self._ok(now); self._init_fresh(now, "paste was empty"); return "initialized"
        if kind == "future":
            self.mode = "readonly"; self.sync["last_error"] = "future_version"; self.sync["next_at"] = now + 900
            log("paste holds a newer doc_version: this service will not touch it"); return "readonly"
        if self.adopt_foreign:
            self._ok(now); self._init_fresh(now, "unrecognised paste adopted (PASTEFY_ADOPT_PASTE)"); return "adopted"
        self.mode = "blocked"; self.sync["last_error"] = kind; self.sync["next_at"] = now + 300
        log("paste content is %s; refusing to overwrite it (set PASTEFY_ADOPT_PASTE=1 to take it over)" % kind)
        return "blocked"

    def _flush(self, now):
        out = self._fetch(now)                     # read-merge-write: another instance may have written meanwhile
        if out is None: return "fail:" + self.sync["last_error"]
        kind, remote, info = out
        if kind == "ok":
            self.bad_streak = 0
            with self.lock: merge_docs(self.doc, remote)
        elif kind == "empty" and self.init_empty:
            pass                                   # the paste we just initialised over (confirmed empty twice): write the first document
        elif kind in ("empty", "corrupt"):
            self.bad_streak += 1
            self.last_flush = now
            if self.bad_streak < self.bad_limit:   # may be transient: never write over it on the first sighting
                self.sync["last_error"] = "suspect_" + kind
                return "suspect-" + kind
            log("paste stayed %s for %d checks: restoring our valid copy" % (kind, self.bad_streak))
        elif kind == "future":
            self.mode = "readonly"; self.sync["last_error"] = "future_version"; return "readonly"
        elif not self.adopt_foreign:               # foreign JSON: probably the wrong paste id
            self.mode = "blocked"; self.sync["last_error"] = "foreign"; self.sync["next_at"] = now + 300; return "blocked"
        with self.lock:
            self.doc["revision"] += 1
            self.doc["updated_at"] = int(now)
            prune_doc(self.doc, now, self.max_bytes)
            text, seq, nrev, cells = dumps(self.doc), self.seq, self.doc["revision"], count_cells(self.doc)
            had_remote_cells = kind == "ok" and count_cells(remote) > 0
        chk, _, _ = parse_doc(text)                # guards: what we are about to write must itself be a valid document
        if chk != "ok" or len(text) > self.max_bytes or (cells == 0 and had_remote_cells):
            self.sync["last_error"] = "refused_write"; self._fail("refused_write", None, now)
            return "refused"
        self.sync["requests"] += 1
        w = self.store.write(text)
        if w.status != "ok":
            self._fail(w.status, w.retry_after, now); return "fail:" + w.status      # dirty stays set
        self.sync["writes"] += 1; self.last_flush = now; self.bad_streak = 0; self.init_empty = False
        with self.lock:
            if self.seq == seq: self.dirty, self.dirty_obs = False, 0
        self._ok(now)
        return self._verify(now, nrev)

    def _verify(self, now, rev):
        out = self._fetch(now)
        if out is not None and out[0] == "ok" and out[1]["revision"] >= rev:
            return "flushed"
        self.sync["verify_fail"] += 1
        with self.lock: self.dirty = True; self.seq += 1
        self.last_flush = now - self.flush_interval + 30     # try again soon
        self.sync["last_error"] = "verify_failed"
        return "unverified"

    def _refresh(self, now):
        self.want_refresh = False
        out = self._fetch(now)
        if out is None: return "fail:" + self.sync["last_error"]
        kind, remote, _ = out
        if kind == "ok":
            with self.lock: merge_docs(self.doc, remote)
            self._ok(now)
        return "refreshed"

    def sync_once(self):
        """One step of the background loop (also called directly by tests). Never raises."""
        if self.store is None: return "volatile"
        if not self._io.acquire(blocking=False): return "busy"
        try:
            now = self.clock()
            if now < self.sync["next_at"]: return "backoff"
            if not self.loaded: return self._load(now)
            if self.mode != "ok": return self.mode
            if self.dirty and (now - self.last_flush >= self.flush_interval or self.dirty_obs >= 200):
                return self._flush(now)
            if self.want_refresh and now - self.last_fetch >= self.cache_ttl:
                return self._refresh(now)
            return "idle"
        except Exception as e:                     # a bug must not kill the loop or leak into responses
            log("sync error: %s" % type(e).__name__); return "error"
        finally:
            self._io.release()

    def shutdown_flush(self):
        """Best effort final write (SIGTERM). Ignores back-off, never writes unless the document was loaded."""
        if self.store is None or not self.loaded or not self.dirty or self.mode != "ok": return "skipped"
        with self._io:
            return self._flush(self.clock())

    def start_background(self, period=5):
        def loop():
            while not self._stop.wait(period):
                self.sync_once()
        self._thread = threading.Thread(target=loop, daemon=True); self._thread.start()

    def stop(self):
        self._stop.set()


def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _send(self, code, body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
            if code >= 400: self.send_header("Connection", "close"); self.close_connection = True   # an unread body must not poison the next request
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
            if n < 0: return self._send(400, {"error": "bad length"})
            if n > MAX_BODY: return self._send(413, {"error": "body too large"})
            try: payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception: return self._send(400, {"error": "malformed json"})
            self._send(*svc.ingest(src, payload))
    return H


def serve(svc, host="0.0.0.0", port=8080):
    srv = ThreadingHTTPServer((host, port), make_handler(svc)); srv.daemon_threads = True; return srv


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def build_service_from_env(env=os.environ):
    tokens = [t for t in env.get("NAV_TOKENS", "").split(",") if t]
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in tokens] + [h for h in env.get("NAV_TOKEN_HASHES", "").split(",") if h]
    if not hashes: sys.exit("refusing to start without NAV_TOKENS / NAV_TOKEN_HASHES (the service is never open)")
    tok, pid = env.get("PASTEFY_API_TOKEN", "").strip(), env.get("PASTEFY_PASTE_ID", "").strip()
    store = None
    if tok and pid:
        try: store = PastefyStore(tok, pid, env.get("PASTEFY_API_BASE", "https://pastefy.app/api/v2"))
        except ValueError as e: sys.exit("bad Pastefy configuration: %s" % e)
    else:
        log("WARNING: PASTEFY_API_TOKEN / PASTEFY_PASTE_ID not set: running VOLATILE (memory is lost on restart)")
    return NavService(store, hashes, min_sources=int(env.get("NAV_MIN_SOURCES", "1")),
                      flush_interval=int(env.get("PASTEFY_FLUSH_SECONDS", "90")), cache_ttl=int(env.get("PASTEFY_CACHE_SECONDS", "300")),
                      max_bytes=int(env.get("PASTEFY_MAX_BYTES", str(DEFAULT_MAX_DOC_BYTES))), adopt_foreign=_flag("PASTEFY_ADOPT_PASTE"))


if __name__ == "__main__":
    svc = build_service_from_env()
    port = int(os.environ.get("PORT", "8080"))
    srv = serve(svc, port=port)
    svc.start_background()
    def _term(*_):
        log("SIGTERM: final flush"); svc.shutdown_flush(); svc.stop(); threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _term)
    log("nav-memory listening on :%d, storage=%s" % (port, "pastefy" if svc.store else "volatile"))
    srv.serve_forever()
