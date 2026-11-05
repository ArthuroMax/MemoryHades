-- Test doubles for the Roblox / executor API. These are SIMULATIONS: nothing here runs in Roblox.
local realTypeof, realOs = typeof, os
local clockNow, timeNow = 1000.0, 1700000000
local os = { time = function() return timeNow end, clock = function() return clockNow end, date = realOs.date }
local function advance(s) clockNow += s timeNow += math.floor(s) end

-- minimal strict JSON (empty table encodes as [] like Roblox)
local JSON = {}
local function enc(v, out)
    local t = type(v)
    if t == "nil" then out[#out + 1] = "null"
    elseif t == "boolean" then out[#out + 1] = tostring(v)
    elseif t == "number" then
        assert(v == v and v ~= math.huge and v ~= -math.huge, "cannot encode non-finite number")
        out[#out + 1] = (v == math.floor(v) and math.abs(v) < 2^53) and string.format("%d", v) or string.format("%.17g", v)
    elseif t == "string" then
        out[#out + 1] = '"' .. v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"'
    elseif t == "table" then
        local n, isArr = 0, true
        for k in pairs(v) do n += 1 end
        for i = 1, n do if v[i] == nil then isArr = false break end end
        if isArr then
            out[#out + 1] = "["
            for i = 1, n do if i > 1 then out[#out + 1] = "," end enc(v[i], out) end
            out[#out + 1] = "]"
        else
            out[#out + 1] = "{"
            local first = true
            local keys = {}
            for k in pairs(v) do keys[#keys + 1] = { tostring(k), k } end
            table.sort(keys, function(a, b) return a[1] < b[1] end)
            for _, kk in ipairs(keys) do
                if not first then out[#out + 1] = "," end
                first = false
                enc(kk[1], out) out[#out + 1] = ":" enc(v[kk[2]], out)
            end
            out[#out + 1] = "}"
        end
    else error("cannot encode " .. t) end
end
function JSON.encode(v) local o = {} enc(v, o) return table.concat(o) end
function JSON.decode(s)
    local i = 1
    local function ws() i = s:find("%S", i) or #s + 1 end
    local val
    local function str()
        local j = i + 1
        local buf = {}
        while true do
            local c = s:sub(j, j)
            if c == "" then error("unterminated string") end
            if c == '"' then i = j + 1 return table.concat(buf) end
            if c == "\\" then
                local n = s:sub(j + 1, j + 1)
                if n == "u" then buf[#buf + 1] = utf8.char(tonumber(s:sub(j + 2, j + 5), 16)) j += 6
                else buf[#buf + 1] = ({ n = "\n", t = "\t", r = "\r", b = "\b", f = "\f" })[n] or n j += 2 end
            else buf[#buf + 1] = c j += 1 end
        end
    end
    function val()
        ws()
        local c = s:sub(i, i)
        if c == "{" then
            local o = {}
            i += 1 ws()
            if s:sub(i, i) == "}" then i += 1 return o end
            while true do
                ws() assert(s:sub(i, i) == '"', "expected key at " .. i)
                local k = str() ws() assert(s:sub(i, i) == ":", "expected : at " .. i) i += 1
                o[k] = val() ws()
                local d = s:sub(i, i) i += 1
                if d == "}" then return o end
                assert(d == ",", "expected , at " .. i)
            end
        elseif c == "[" then
            local a = {}
            i += 1 ws()
            if s:sub(i, i) == "]" then i += 1 return a end
            while true do
                a[#a + 1] = val() ws()
                local d = s:sub(i, i) i += 1
                if d == "]" then return a end
                assert(d == ",", "expected , at " .. i)
            end
        elseif c == '"' then return str()
        elseif s:sub(i, i + 3) == "true" then i += 4 return true
        elseif s:sub(i, i + 4) == "false" then i += 5 return false
        elseif s:sub(i, i + 3) == "null" then i += 4 return nil
        else
            local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", i)
            assert(num and #num > 0, "bad json at " .. i)
            i += #num
            return tonumber(num)
        end
    end
    local r = val() ws()
    assert(i > #s, "trailing garbage")
    return r
end
local guidN = 0
local HttpService = {
    JSONEncode = function(_, v) return JSON.encode(v) end,
    JSONDecode = function(_, s) return JSON.decode(s) end,
    UrlEncode = function(_, s) return (s:gsub("[^%w%-_%.~]", function(c) return string.format("%%%02X", c:byte()) end)) end,
    GenerateGUID = function() guidN += 1 return string.format("00000000-0000-0000-0000-%012d", guidN) end,
}
local function typeof(v) if type(v) == "table" and v.__V3 then return "Vector3" end return realTypeof(v) end
local function V3(x, y, z) return { __V3 = true, X = x, Y = y, Z = z } end

-- cooperative "task" so a request can be left hanging and answered later
local parked = {}
local task = { spawn = function(fn, ...)
    local co = coroutine.create(fn)
    local ok, err = coroutine.resume(co, ...)
    if not ok then error(err, 0) end
    if coroutine.status(co) == "suspended" then parked[#parked + 1] = co end
end }
local function resumeParked()
    local list = parked
    parked = {}
    for _, co in ipairs(list) do
        local ok, err = coroutine.resume(co)
        if not ok then error(err, 0) end
        if coroutine.status(co) == "suspended" then parked[#parked + 1] = co end
    end
end

-- in-memory file system (with failure injection)
local FS, fsFail = {}, { write = false }
local function writefile(p, c) if fsFail.write then error("disk full") end FS[p] = c end
local function readfile(p) if FS[p] == nil then error("no such file") end return FS[p] end
local function isfile(p) return FS[p] ~= nil end

-- simulated Render service (what the Lua client talks to)
local Srv = { mode = "ok", reqs = {}, advice = {}, accepted = {}, batches = {}, rev = 1, mid = "mem0001", state = "synced", hold = nil }
local function request(opts)
    Srv.reqs[#Srv.reqs + 1] = { url = opts.Url, method = opts.Method, headers = opts.Headers, body = opts.Body }
    local path = opts.Url:gsub("^https?://[^/]+", "")
    if Srv.mode == "down" then error("connection refused") end
    if Srv.mode == "hang" then Srv.hold = true coroutine.yield() end
    if path == "/healthz" then
        if Srv.mode == "asleep" then return { StatusCode = 502, Body = "", Headers = {} } end
        return { StatusCode = 200, Body = JSON.encode({ ok = true, storage = { state = Srv.state } }), Headers = {} }
    end
    if Srv.mode == "asleep" then return { StatusCode = 502, Body = "", Headers = {} } end
    if Srv.mode == "429" then return { StatusCode = 429, Body = "{}", Headers = { ["Retry-After"] = "120" } } end
    if Srv.mode == "503" then return { StatusCode = 503, Body = "{}", Headers = {} } end
    if Srv.mode == "garbage" then return { StatusCode = 200, Body = "<html>", Headers = {} } end
    if opts.Method == "POST" then
        local b = JSON.decode(opts.Body)
        if Srv.batches[b.batch_id] then return { StatusCode = 200, Body = JSON.encode({ schema_version = 1, accepted = #b.observations, duplicate = true }), Headers = {} } end
        Srv.batches[b.batch_id] = true
        for _, o in ipairs(b.observations) do Srv.accepted[#Srv.accepted + 1] = o end
        if Srv.mode == "lostreply" then return { StatusCode = 500, Body = "", Headers = {} } end
        return { StatusCode = 200, Body = JSON.encode({ schema_version = 1, accepted = #b.observations }), Headers = {} }
    end
    if Srv.mode == "badrecords" then
        return { StatusCode = 200, Body = JSON.encode({ schema_version = 1, server_time = timeNow, revision = Srv.rev, memory_id = Srv.mid,
            records = { { kind = "teleport", state = "candidate", x = 1, z = 1, confidence = 0.9, expires = timeNow + 100, observations = 3 } } }), Headers = {} }
    end
    return { StatusCode = 200, Body = JSON.encode({ schema_version = 1, server_time = timeNow, revision = Srv.rev, memory_id = Srv.mid, records = Srv.advice }), Headers = {} }
end
local function resetSrv() Srv.mode, Srv.reqs, Srv.advice, Srv.accepted, Srv.batches, Srv.hold = "ok", {}, {}, {}, {}, nil end
local function countReq(method, pattern)
    local n = 0
    for _, r in ipairs(Srv.reqs) do if r.method == method and (not pattern or r.url:find(pattern, 1, true)) then n += 1 end end
    return n
end
