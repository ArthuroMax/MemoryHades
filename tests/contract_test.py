#!/usr/bin/env python3
"""Wire-contract test: what the REAL Lua client code emits is accepted by the REAL server.py, and what server.py answers is
accepted by the Lua client. (Both sides are the delivered code; the network between them is simulated by this script.)"""
import hashlib, json, os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import server as S
LUAU = os.environ.get("LUAU", "luau")
src = open(os.environ.get("COINFARM_LUA", os.path.join(HERE, "..", "CoinFarm_v9_hybridfix_4.lua")), encoding="utf-8").read()
mem = src[src.index("-- MEM-BEGIN"):src.index("-- MEM-END")]
stubs = open(os.path.join(HERE, "lua", "stubs.lua")).read()
PRE = '''
local traces = {}
local function newClient(S)
    local CoinFarm = { trace = function(...) end }
    local running = true
    local container = nil
    local game = { PlaceId = 123 }
''' + mem + '''
    return CoinFarm.mem
end
'''
def luau(body):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.lua"); open(p, "w").write(stubs + PRE + body)
        r = subprocess.run([LUAU, p], capture_output=True, text=True); return r.returncode, r.stdout, r.stderr
R = []
def check(n, c, d=""): R.append(bool(c)); print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))
print("== W. wire contract: Lua client <-> server.py ==")
rc, out, err = luau('''
local M = newClient({ MemUrl = "https://x.example", MemToken = "tok" }); M.setMap("123:Diner", "100x50x100")
for i = 1, 3 do M.record("route", V3(10, 3, 10), "success", "ok", { nav = "COLLECTING" }) end
for i = 1, 3 do M.record("hop", V3(50, 3, 50), "fail", "missed", { nav = "COLLECTING", recovery = "replan" }) end
M.record("escape", V3(90, 3, 90), "unsafe", "he followed", { nav = "ESCAPING", escape = true })
for i = 1, 5 do advance(1) M.tick(os.clock()) end
for _, r in ipairs(Srv.reqs) do if r.method == "POST" then print("WIRE:" .. r.body) end end
''')
wire = [l[5:] for l in out.splitlines() if l.startswith("WIRE:")]
check("W1 the client produced a batch upload", rc == 0 and len(wire) == 1, err[:300])
svc = S.NavService(None, [hashlib.sha256(b"tok").hexdigest()], clock=lambda: 1700000010.0)
payload = json.loads(wire[0]) if wire else {}
code, body = svc.ingest("abcdef0123456789", payload)
check("W2 server.py accepts the client's real payload (schema, batch_id, field validation) -> 200, 7 accepted", code == 200 and body.get("accepted") == 7, str((code, body)))
check("W3 the payload carries a batch_id and the same schema_version", payload.get("schema_version") == 1 and S.BATCH_RE.match(payload.get("batch_id", "")))
code2, body2 = svc.ingest("abcdef0123456789", payload)
check("W4 re-sending the identical batch is recognised as a duplicate", code2 == 200 and body2.get("duplicate") is True and svc.doc["maps"]["123:Diner"]["cells"]["route|2|2"]["n"] == 3)
code, adv = svc.advice("123:Diner")
states = {(r["kind"], r["x"], r["z"]): r["state"] for r in adv["records"]}
check("W5 server advice derived from the client's observations: route candidate, hop unreliable, escape invalid", states == {("route", 10, 10): "candidate", ("hop", 50, 50): "unreliable", ("escape", 90, 90): "invalid"}, str(states))
print("        server advice:", states)
rc, out, err = luau('''
local M = newClient({ MemUrl = "https://x.example", MemToken = "tok" }); M.setMap("123:Diner", "100x50x100")
local adv = JSON.decode([==[''' + json.dumps(adv) + ''']==])
local n, why = M.ingest("123:Diner", adv)
print("N:" .. tostring(n) .. ":" .. tostring(why))
print("ST_ROUTE:" .. M.state("route", V3(10, 3, 10)))
print("ST_ESC:" .. M.state("escape", V3(90, 3, 90)))
print("REV:" .. tostring(M.remoteMeta["123:Diner"].rev) .. ":" .. tostring(M.remoteMeta["123:Diner"].mid))
''')
lines = dict(l.split(":", 1) for l in out.splitlines() if ":" in l)
exp_n = len(adv["records"])
check("W6 the Lua client accepts every record of the real server answer (no field rejected)", rc == 0 and lines.get("N", "").split(":")[0] == str(exp_n), out + err[:200])
check("W6b a client with no local cells of its own acts on the server advice (candidate route, invalid escape)", lines.get("ST_ROUTE") == "candidate" and lines.get("ST_ESC") == "invalid", str(lines))
check("W7 revision and memory_id are picked up for the stale-answer guard", lines.get("REV", "").endswith(":" + adv["memory_id"]), lines.get("REV"))
print("\nwire summary: %d / %d passed" % (sum(R), len(R))); sys.exit(0 if all(R) else 1)
