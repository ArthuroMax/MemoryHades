-- ===== tests for the navigation-memory block (extracted from the script between MEM-BEGIN / MEM-END) =====
local results = {}
local function check(name, cond, detail)
    results[#results + 1] = { name, cond and true or false }
    print((cond and "  PASS  " or "  FAIL  ") .. name .. ((not cond and detail) and ("   [" .. tostring(detail) .. "]") or ""))
end
local traces = {}
local function newClient(S)
    local CoinFarm = { trace = function(...) traces[#traces + 1] = { ... } end }
    local running = true
    local container = nil
    local game = { PlaceId = 123 }
    --MEM_BLOCK--
    CoinFarm.stop = function() running = false end
    return CoinFarm.mem, CoinFarm
end
local function settings(t) local s = { MemUrl = "https://memoryhades.onrender.com", MemToken = "tok" } for k, v in pairs(t or {}) do s[k] = v end return s end
local function reset() FS = {} resetSrv() fsFail.write = false end
local function count(t) local n = 0 for _ in pairs(t) do n += 1 end return n end
local function rec(M, kind, x, z, outcome, n) for _ = 1, n or 1 do M.record(kind, V3(x, 3, z), outcome, "r", { nav = "COLLECTING" }) end end

print("== L1. learning rules unchanged (decay, thresholds, contradiction, map change) ==")
reset()
local M = newClient(settings()); M.setMap("123:Diner", "100x50x100")
rec(M, "route", 10, 10, "success", 2); local a = M.state("route", V3(10, 3, 10)); rec(M, "route", 10, 10, "success"); local b = M.state("route", V3(10, 3, 10))
check("L1.1 two successes unknown, three candidate", a == "unknown" and b == "candidate", a .. "," .. b)
advance(8 * 86400); check("L1.2 candidate lapses after 7 days", M.state("route", V3(10, 3, 10)) == "unknown")
local M2 = newClient(settings()); M2.setMap("123:Diner", "100x50x100")
rec(M2, "stuck", 50, 50, "fail", 6); local u1 = M2.state("stuck", V3(50, 3, 50)); rec(M2, "stuck", 50, 50, "success")
check("L1.3 contradiction: one success drops 'unreliable' without jumping to candidate", u1 == "unreliable" and M2.state("stuck", V3(50, 3, 50)) == "unknown")
local M3 = newClient(settings()); M3.setMap("123:Diner", "100x50x100"); rec(M3, "route", 10, 10, "success", 4); M3.setMap("123:Diner", "300x50x300")
check("L1.4 map-change detection weakens remembered cells to a quarter", M3.state("route", V3(10, 3, 10)) == "unknown" and M3.maps["123:Diner"].sig == "300x50x300")
local M4 = newClient(settings()); M4.setMap("123:Diner", "x"); rec(M4, "escape", 10, 10, "success", 4); rec(M4, "escape", 10, 10, "unsafe")
check("L1.5 an unsafe escape destination is invalid at once", M4.state("escape", V3(10, 3, 10)) == "invalid" and M4.avoid("escape", V3(10, 3, 10)))

print("\n== L2. JSON serialization + local persistence (restart) ==")
reset()
local A = newClient(settings()); A.setMap("123:Diner", "100x50x100")
rec(A, "route", 10, 10, "success", 4); rec(A, "hop", 30, 30, "fail", 3); rec(A, "coin", 70, 70, "abandon", 3); rec(A, "escape", 90, 90, "success", 4)
Srv.advice = { { kind = "hop", x = 202, z = 202, state = "unreliable", confidence = 0.1, expires = timeNow + 3600, observations = 5 } }
A.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 7, memory_id = "mem0001", records = Srv.advice })
local saved = A.save()
local rawFile = FS["MM2CoinFarm_NavMemory.json"]
local decoded = JSON.decode(rawFile)
check("L2.1 save writes valid versioned JSON (schema, maps, queue, remote)", saved and decoded.schema == 1 and type(decoded.maps) == "table" and type(decoded.queue) == "table" and type(decoded.remote) == "table")
check("L2.2 serialize -> deserialize -> serialize is stable", JSON.encode(JSON.decode(rawFile)) == rawFile)
local B = newClient(settings()); B.setMap("123:Diner", "100x50x100")
check("L2.3 a restarted client reloads cells, queue and cached server advice", B.loadInfo == "file" and count(B.maps["123:Diner"].cells) == 4 and #B.queue == #A.queue and B.remote["123:Diner"] ~= nil and B.remoteMeta["123:Diner"].rev == 7)
check("L2.4 reloaded memory gives the same verdicts", B.state("route", V3(10, 3, 10)) == "candidate" and B.state("hop", V3(202, 3, 202)) == "unreliable" and B.state("coin", V3(70, 3, 70)) == "unreachable")

print("\n== L3. Render unavailable: local memory keeps working ==")
Srv.mode = "down"
local C = newClient(settings()); C.setMap("123:Diner", "100x50x100")
local okAll = true
for i = 1, 40 do advance(1) local ok = pcall(C.tick, os.clock()) okAll = okAll and ok end
check("L3.1 tick never throws while the server is down", okAll)
check("L3.2 previously downloaded + local memory remain usable offline", C.state("route", V3(10, 3, 10)) == "candidate" and C.state("hop", V3(202, 3, 202)) == "unreliable")
check("L3.3 the unsent queue is intact and status says local memory is in use", #C.queue >= 8 and C.status():find("local memory in use", 1, true) ~= nil and C.net.fails >= 1, C.status())
check("L3.4 a failed sync never erased anything (file still decodes with the same cells)", count(JSON.decode(FS["MM2CoinFarm_NavMemory.json"]).maps["123:Diner"].cells) == 4)
local reqsDown = #Srv.reqs
for i = 1, 600 do advance(1) pcall(C.tick, os.clock()) end
check("L3.5 outage back-off: 10 more minutes of ticking sends only a handful of probes (no hammering, no token sent)", #Srv.reqs - reqsDown <= 8 and countReq("POST") == 0, #Srv.reqs - reqsDown)

print("\n== L4. recovery when Render returns ==")
Srv.mode = "ok"
for i = 1, 700 do advance(1) pcall(C.tick, os.clock()) if C.net.fails == 0 and #C.queue == 0 then break end end
check("L4.1 sync resumes: every queued observation was delivered exactly once, queue empty", #Srv.accepted == #A.queue and #C.queue == 0 and C.net.fails == 0, #Srv.accepted .. "/" .. #A.queue .. " q=" .. #C.queue .. " fails=" .. C.net.fails .. " err=" .. tostring(C.net.lastErr))
check("L4.2 fresh advice was pulled and cached", C.remoteMeta["123:Diner"] ~= nil and C.remoteMeta["123:Diner"].fetched > 0)
local tokenOnlyToServer = true
for _, r in ipairs(Srv.reqs) do
    if r.url:find("/healthz", 1, true) and r.headers["Authorization"] then tokenOnlyToServer = false end
    if not r.url:find("https://memoryhades.onrender.com", 1, true) then tokenOnlyToServer = false end
end
check("L4.3 the healthz wake-up probe carries no token; all traffic goes to the configured https service", tokenOnlyToServer)

print("\n== L5. corrupted files / responses can never destroy valid memory ==")
reset()
local G = newClient(settings()); G.setMap("123:Diner", "100x50x100"); rec(G, "route", 10, 10, "success", 4); G.save(); G.bakAt = -1e9; G.save()
local goodRaw = FS["MM2CoinFarm_NavMemory.json"]
FS["MM2CoinFarm_NavMemory.json"] = goodRaw:sub(1, #goodRaw // 2)
local H = newClient(settings()); H.setMap("123:Diner", "100x50x100")
check("L5.1 truncated main file: memory restored from the backup snapshot", H.loadInfo == "backup" and H.state("route", V3(10, 3, 10)) == "candidate", H.loadInfo)
H.save()
check("L5.2 the damaged file is preserved as .corrupt, not silently overwritten", FS["MM2CoinFarm_NavMemory.json.corrupt"] == goodRaw:sub(1, #goodRaw // 2))
reset(); FS["MM2CoinFarm_NavMemory.json"] = "{not json at all"; FS["MM2CoinFarm_NavMemory.json.bak"] = "also bad"
local J = newClient(settings()); J.setMap("123:Diner", "x"); rec(J, "coin", 5, 5, "success")
check("L5.3 unreadable main + backup: starts empty without crashing and flags it", J.loadInfo:find("unreadable", 1, true) ~= nil)
J.save()
check("L5.4 ...but the unreadable original was kept (.corrupt) before being replaced", FS["MM2CoinFarm_NavMemory.json.corrupt"] == "{not json at all")
reset(); FS["MM2CoinFarm_NavMemory.json"] = JSON.encode({ schema = 99, maps = {} })
local K = newClient(settings())
check("L5.5 a file from another schema version is not trusted", K.loadInfo:find("unreadable", 1, true) ~= nil and count(K.maps) == 0)
reset(); FS["MM2CoinFarm_NavMemory.json"] = JSON.encode({ schema = 1, maps = { m = { cells = { ["route|1|1"] = { s = -5, f = 0, a = 0, u = 0, n = 1, t0 = 1, t1 = 1 }, ["route|2|2"] = { s = 1, f = 0, a = 0, u = 0, n = 1, t0 = 1, t1 = 1 } } } },
    queue = { { v = 1, map = "m", kind = "teleport" }, "x" }, remote = { m = { recs = { ["hop|1|1"] = { state = "candidate", conf = 2, expires = timeNow + 9, obs = 1 } } } } })
local L = newClient(settings())
check("L5.6 invalid cells / queue entries / advice inside a file are dropped individually", count(L.maps.m.cells) == 1 and #L.queue == 0 and next(L.remote.m or {}) == nil)
reset()
local P = newClient(settings()); P.setMap("123:Diner", "x"); rec(P, "route", 10, 10, "success", 4)
Srv.advice = { { kind = "route", x = 10, z = 10, state = "candidate", confidence = 0.9, expires = timeNow + 9999, observations = 4 } }
for i = 1, 5 do advance(1) P.tick(os.clock()) end
local before = P.remote["123:Diner"]
local metaRev = P.remoteMeta["123:Diner"].fetched
local function badAnswerKeepsCache(mode)
    Srv.mode = mode
    P.remoteMeta["123:Diner"].fetched = 0 -- force a pull
    P.net.nextAt, P.net.awake = 0, true
    advance(1) P.tick(os.clock())
    local same = P.remote["123:Diner"] == before and count(P.remote["123:Diner"]) == 1
    P.net.nextAt = 0
    Srv.mode = "ok"
    return same
end
check("L5.7 html / invalid-record / 503 answers leave the cached advice untouched", badAnswerKeepsCache("garbage") and badAnswerKeepsCache("badrecords") and badAnswerKeepsCache("503"))
P.remoteMeta["123:Diner"].fetched = 0
check("L5.8 an answer with an OLDER revision of the same memory is ignored (stale)", select(2, P.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 0, memory_id = "mem0001", records = {} })) == "stale" and P.remote["123:Diner"] == before)
P.remoteMeta["123:Diner"] = { rev = 9, mid = "mem0001", fetched = os.time() }
local nn, why = P.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 3, memory_id = "mem0001", records = {} })
check("L5.9 older revision rejected twice, accepted when the service insists (rollback) ; a different memory_id is accepted at once",
    nn == nil and why == "stale" and select(2, P.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 3, memory_id = "mem0001", records = {} })) == "stale"
    and P.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 3, memory_id = "mem0001", records = {} }) == 0
    and P.remoteMeta["123:Diner"].rev == 3)
P.remoteMeta["123:Diner"] = { rev = 9, mid = "mem0001", fetched = os.time() }
check("L5.9b a different memory_id (paste replaced) is accepted at once", P.ingest("123:Diner", { schema_version = 1, server_time = timeNow, revision = 1, memory_id = "other", records = {} }) == 0)
fsFail.write = true
check("L5.10 a failing disk write is reported, never throws, in-memory memory survives", P.save() == false and P.state("route", V3(10, 3, 10)) == "candidate")

print("\n== L6. single request in flight, hung requests, lost replies (no duplicates, no leaks) ==")
reset()
local Q = newClient(settings()); Q.setMap("123:Diner", "x"); rec(Q, "route", 10, 10, "success", 3)
Srv.mode = "hang"
advance(1) Q.tick(os.clock())
for i = 1, 18 do advance(1) Q.tick(os.clock()) end
check("L6.1 while a request hangs no second request is started", #Srv.reqs == 1, #Srv.reqs)
advance(5) Q.tick(os.clock())
check("L6.2 after the 20 s watchdog it is a failure with back-off (not an instant second request)", Q.net.fails == 1 and #Srv.reqs == 1 and Q.net.busy == nil, Q.net.fails .. "/" .. #Srv.reqs)
Srv.mode = "ok"; resumeParked()
check("L6.3 the late answer of the abandoned request is discarded (state unchanged)", Q.net.fails == 1 and Q.net.busy == nil)
for i = 1, 200 do advance(1) Q.tick(os.clock()) resumeParked() if #Q.queue == 0 then break end end
check("L6.4 the retry reuses the same batch id: the server applied the observations exactly once", #Srv.accepted == 3 and #Q.queue == 0, #Srv.accepted)
reset()
local Lr = newClient(settings()); Lr.setMap("123:Diner", "x"); rec(Lr, "route", 10, 10, "success", 3)
Srv.mode = "lostreply"
for i = 1, 3 do advance(1) Lr.tick(os.clock()) end
check("L6.5 server applied the batch but the reply was lost: the client keeps its queue", #Lr.queue == 3 and Lr.inflight ~= nil and #Srv.accepted == 3)
Srv.mode = "ok"
for i = 1, 400 do advance(1) Lr.tick(os.clock()) if #Lr.queue == 0 then break end end
check("L6.6 the retry is recognised as a duplicate by batch id: still exactly 3 on the server, queue cleared", #Srv.accepted == 3 and #Lr.queue == 0)
Lr.record("route", V3(10, 3, 10), "success", "late")
check("L6.7 observations recorded during a retry are not lost when the older batch is acknowledged", #Lr.queue == 1)

print("\n== L7. sleeping Render, rate limits, configuration ==")
reset(); Srv.mode = "asleep"
local R = newClient(settings()); R.setMap("123:Diner", "x"); rec(R, "route", 10, 10, "success", 3)
for i = 1, 300 do advance(1) R.tick(os.clock()) end
check("L7.1 while the service sleeps only the tokenless /healthz probe is sent (no bearer token, no observation upload)", countReq("POST") == 0 and countReq("GET", "/v1/") == 0 and countReq("GET", "/healthz") >= 1 and countReq("GET", "/healthz") <= 6, countReq("GET", "/healthz"))
Srv.mode = "429"; local S2 = newClient(settings()); S2.setMap("123:Diner", "x"); rec(S2, "route", 10, 10, "success", 3); S2.net.awake = true
advance(1) S2.tick(os.clock()); local t0 = os.clock()
check("L7.2 a 429 Retry-After of 120 s is honoured", S2.net.nextAt - t0 >= 119, S2.net.nextAt - t0)
reset(); local T = newClient(settings({ MemUrl = "http://insecure.example" })); T.setMap("123:Diner", "x"); rec(T, "route", 10, 10, "success", 3)
for i = 1, 30 do advance(1) T.tick(os.clock()) end
check("L7.3 a plain-http URL is never used (the token would travel in clear): no requests, status explains", #Srv.reqs == 0 and T.status():find("https://", 1, true) ~= nil)
reset(); local U = newClient(settings({ MemToken = "" })); U.setMap("123:Diner", "x"); rec(U, "route", 10, 10, "success", 3)
for i = 1, 30 do advance(1) U.tick(os.clock()) end
check("L7.4 no token = local only: zero requests", #Srv.reqs == 0 and U.status():find("local only", 1, true) ~= nil)
reset(); local V = newClient(settings({ MemUrl = "" })); V.setMap("123:Diner", "x")
for i = 1, 5 do advance(1) V.tick(os.clock()) end
check("L7.5 an empty URL falls back to the default Render deployment", #Srv.reqs >= 1 and Srv.reqs[1].url:find("https://memoryhades.onrender.com", 1, true) == 1)

print("\n== L8. request economy / cache ==")
reset(); Srv.advice = {}
local W = newClient(settings()); W.setMap("123:Diner", "x")
for i = 1, 5 do advance(1) W.tick(os.clock()) end
local n1 = #Srv.reqs
for i = 1, 500 do advance(1) W.tick(os.clock()) end
check("L8.1 once synced, 500 s of ticking makes no further request (advice cached 10 min, nothing queued)", #Srv.reqs == n1 and n1 == 2, n1 .. "/" .. #Srv.reqs)
for i = 1, 150 do advance(1) W.tick(os.clock()) end
check("L8.2 after the cache ages out ONE advice request is made (no healthz repeat while healthy)", #Srv.reqs == n1 + 1 and countReq("GET", "/healthz") == 1, #Srv.reqs - n1)
W.save()
local W2 = newClient(settings()); W2.setMap("123:Diner", "x")
local nb = #Srv.reqs
for i = 1, 20 do advance(1) W2.tick(os.clock()) end
check("L8.3 rejoining with a fresh cached copy of the map's advice does not download it again", countReq("GET", "/v1/maps") == countReq("GET", "/v1/maps") and #Srv.reqs - nb <= 1, #Srv.reqs - nb)
reset(); local X = newClient(settings()); X.setMap("123:Diner", "x")
for i = 1, 3 do advance(1) X.tick(os.clock()) end
for i = 1, 25 do rec(X, "coin", 10 + 4 * i, 10, "success") end
for i = 1, 5 do advance(1) X.tick(os.clock()) end
check("L8.4 a queue of 25 goes out in ONE batch request", countReq("POST") == 1 and #Srv.accepted == 25, countReq("POST"))

print("\n== L9. lifecycle ==")
reset()
local Y, YF = newClient(settings()); Y.setMap("123:Diner", "x"); rec(Y, "route", 10, 10, "success", 3)
Srv.mode = "hang"; advance(1) Y.tick(os.clock())
local genBefore = Y.gen
local sOk = Y.shutdown()
Srv.mode = "ok"; YF.stop(); resumeParked()
check("L9.1 shutdown saves once, invalidates the in-flight request and a late answer changes nothing", sOk and Y.gen == genBefore + 1 and #Y.queue == 3 and Y.net.busy == nil)
check("L9.2 the saved file holds the unsent queue (delivered after the next start)", #JSON.decode(FS["MM2CoinFarm_NavMemory.json"]).queue == 3)
local Z = newClient(settings()); Z.setMap("123:Diner", "x")
for i = 1, 400 do advance(1) Z.tick(os.clock()) if #Z.queue == 0 then break end end
check("L9.3 next session delivers it (sync resumed after restart)", #Z.queue == 0 and #Srv.accepted >= 3)

local pass = 0
for _, r in ipairs(results) do if r[2] then pass += 1 end end
print(("\nlua memory summary: %d / %d passed"):format(pass, #results))
if pass ~= #results then error("failures") end
