# Navigation-memory service v2 — Pastefy storage

Status: **implemented and tested against a SIMULATED Pastefy and a SIMULATED Roblox/Render environment. Not deployed. Never run against the real Pastefy API, never run in Roblox.**
Python standard library only. See §9 for exactly what was and was not tested.

## 1. What changed
SQLite is gone. The service keeps one versioned JSON document in memory and persists it to **one designated Pastefy paste** (`GET`/`PUT https://pastefy.app/api/v2/paste/{id}`, `Authorization: Bearer <token>` — checked against docs.pastefy.app; PUT returns 204). It never creates pastes. Because storage is external, **no persistent disk and no paid Render plan are required**.

## 2. Architecture and the offline guarantee
```
 Roblox script (local-first)                Render service (this repo)                  Pastefy
 ┌──────────────────────────────┐  HTTPS   ┌───────────────────────────────┐  HTTPS   ┌─────────────────┐
 │ local file: own cells, unsent │ ───────▶ │ in-memory document (cache)     │ ───────▶ │ ONE paste (JSON) │
 │ queue, cached server advice   │ ◀─────── │ batched write, read-merge-write│ ◀─────── │ revisioned doc   │
 │ never needs the service       │ own token│ holds the Pastefy token        │ API token└─────────────────┘
 └──────────────────────────────┘          └───────────────────────────────┘
```
| Situation | Behaviour |
|---|---|
| Render up, Pastefy up | script pushes observations (batched, idempotent), pulls advice per map (cached 10 min); service flushes to Pastefy every ≤90 s |
| **Render asleep / down** | script keeps playing from its **local file** (own cells + the last advice it downloaded, each record valid until its own expiry). Only a tokenless `GET /healthz` wake-up probe is sent, with exponential back-off 30 s → 10 min. Nothing local is ever deleted by a failed sync |
| Render comes back | the next probe succeeds, the persisted queue is delivered (same batch id on retries → no double counting), advice is refreshed |
| Pastefy down / rate limited / 5xx | service keeps serving from memory, stays dirty, retries with back-off (honours `Retry-After`); `/healthz` stays 200 and says `degraded` |
| Render restarts while Pastefy is down | service refuses uploads/advice with 503 until the document is loaded; clients keep queueing locally. **It never writes before it has loaded.** |

**Limits, stated plainly.** A sleeping Render service cannot proxy anything, so while it sleeps the script cannot reach Pastefy *through* it. Observations the service has accepted but not yet flushed (≤ one flush interval, longer during a Pastefy outage) are lost if Render is killed in that window; the clients' own local memory is unaffected. On a free plan the first request after sleep takes tens of seconds (the script never blocks on it).

### Why the client does not talk to Pastefy directly
Pastefy only offers Bearer API tokens. Anything inside a Roblox script can be extracted by whoever has the script, and I could not verify that Pastefy tokens can be scoped (e.g. to one paste or to write-only) — so a token in the client would let anyone overwrite or read your account's pastes. That is why the token lives only in Render's environment. If you ever want Render-independent sync, the safe options are: (a) a read-only copy of the memory in a **public/unlisted paste** fetched without a token (read-only, but then the memory is public; I did not implement it), or (b) a second tiny always-on proxy. I did not implement either.

## 3. Configuration (Render → Environment)
| Variable | Required | Meaning |
|---|---|---|
| `PASTEFY_API_TOKEN` | yes | token from <https://pastefy.app/apikeys>. Never in code, never logged, never returned |
| `PASTEFY_PASTE_ID` | yes | id of the designated paste. Create it **once** by hand (content `{}` is fine, visibility **PRIVATE**) |
| `NAV_TOKENS` / `NAV_TOKEN_HASHES` | yes | secrets the Lua script sends as *Memory token* (hashes: `sha256` hex, comma separated) |
| `PASTEFY_API_BASE` | no | default `https://pastefy.app/api/v2` (must be https; http only for localhost tests) |
| `PASTEFY_FLUSH_SECONDS` | no | batch interval for writes, default 90 |
| `PASTEFY_CACHE_SECONDS` | no | background refresh age (merge in changes from elsewhere), default 300 |
| `PASTEFY_MAX_BYTES` | no | document size cap, default 600000 (Pastefy documents no limit; older low-value cells are pruned first) |
| `PASTEFY_ADOPT_PASTE` | no | `1` = allow overwriting a paste whose content is not ours. Off by default (protects against a wrong id) |
| `NAV_MIN_SOURCES` | no | distinct contributors needed before a state is advised (default 1; use ≥ 2 for a public deployment) |
Without the two `PASTEFY_*` variables the service still starts but is **VOLATILE** (memory lost on restart); `/healthz` says so.

## 4. Document stored in the paste (`doc_version` 1)
```json
{"format":"coinfarm-navmemory","doc_version":1,"memory_id":"<random>","revision":17,"updated_at":1700000000,
 "maps":{"<placeId>:<map>":{"sig":"100x50x100","ts":1700000000,
   "cells":{"<kind>|<cx>|<cz>":{"s":3,"f":0,"a":0,"u":0,"n":3,"t0":..,"t1":..,"y":3.0,"lu":..,"why":"stuck","src":["1a2b3c4d"]}}}},
 "recent_batches":["<batch id>", "..."]}
```
Kinds: `hop` (jump/platform hops), `route`, `stuck`, `escape` (learned escape destinations), `coin` (coin locations). `sig` is the map-size signature (map information). 4-stud cells; `s/f/a/u` are decayed success/fail/abandon/unsafe counts; `src` are 8-hex hashes of contributor tokens (never the token). A newer `doc_version` than this code knows puts the service in **read-only** mode (it will not touch the paste). Add a function to `MIGRATIONS` before raising `DOC_VERSION`.

## 5. Learning rules (unchanged from v1; identical in `server.py` and the Lua `Mem.stateOf`)
14-day half-life decay; a contradicting observation halves the opposing history; **a changed map signature multiplies everything remembered for that map by 0.25** (v1 applied it per touched row; it is now per map, matching what the Lua client already did — a client whose signature flips back and forth would weaken a map repeatedly, as before).
| state | needs | lasts |
|---|---|---|
| `unreachable` | ≥ 2.5 abandons, ~no success | 6 h, then retested |
| `unreliable` | ≥ 2.5 failures and confidence ≤ 0.3 | 24 h |
| `candidate` | ≥ 2.5 successes and confidence ≥ 0.75 | 7 days |
| `invalid` (escape) | any `unsafe` | 30 min, then must be re-earned |

## 6. Safety rules for the stored memory
- **Load before write.** Nothing is written until the paste has been read and validated.
- **Empty paste**: initialised only after *two* empty answers (one blank could be a glitch over real data).
- **Corrupt / HTML / truncated / foreign / newer-version / missing-`content` answers** are never merged and never trigger a write; a mostly-invalid document (>10 % bad cells) counts as corrupt; a few bad cells are dropped individually.
- If the paste *becomes* empty/corrupt after we loaded valid memory, it is restored from our valid copy only after **3 consecutive bad checks**; foreign JSON (probably the wrong paste id) is never overwritten unless `PASTEFY_ADOPT_PASTE=1`.
- **Read-merge-write**: every flush re-reads the paste and merges it (newest cell wins) so two instances (overlapping deploys) do not erase each other; the written text is re-parsed and size-checked first; the write is read back to verify.
- Redirects are not followed (the Authorization header could leak); errors never contain the token.
- Ingest is idempotent per `batch_id`. Validation, ranges, 64 KiB bodies, 50 observations per batch and per-token rate limits are as in v1.

## 7. API
`GET /healthz` (no auth; always 200 while the process runs — a failing health check would make Render restart the service; look at `storage.state`: `synced|pending|degraded|loading|blocked|readonly|volatile`, `warnings` e.g. `paste_is_public`).
`POST /v1/observations` and `GET /v1/maps/<id>/advice` as before (`schema_version` 1). New: optional `batch_id` on POST; `revision` + `memory_id` in advice; `503 {"retry":true}` while the memory is not available (the client keeps its queue).

## 8. Deploying to the existing service
1. On Pastefy create one paste (content `{}`, visibility PRIVATE) and an API key; note the paste id.
2. Render dashboard → *memoryhades* → Environment: set `PASTEFY_API_TOKEN`, `PASTEFY_PASTE_ID`, `NAV_TOKENS`.
3. Deploy `server.py` (start command `python server.py`, health check `/healthz`). Remove the old disk/`NAV_DB_PATH` if you had one. `render.yaml` is an untested reference.
4. Optional: `PASTEFY_API_TOKEN=… PASTEFY_PASTE_ID=… python tests/live_pastefy_smoke.py [--write]` verifies the real Pastefy contract from your machine.
5. In the script: *Memory token* = one of `NAV_TOKENS` (URL defaults to `https://memoryhades.onrender.com`).
6. Optional UptimeRobot on `/healthz` (5 min) keeps a free instance awake but uses instance-hours; it is a monitor, not a guarantee.
Old SQLite data is **not migrated** (no tool written); the clients' local memory is unaffected and rebuilds the shared memory over time.

## 9. Test status — simulated vs real
| What | How | Result |
|---|---|---|
| Backend logic, Pastefy transport, failure paths (outage, 429, 5xx, junk, corruption, restart, merge, caching, token secrecy) | `tests/test_backend.py` against `tests/fake_pastefy.py` (**simulated** Pastefy built from docs.pastefy.app) | 79/79 |
| Lua memory (persistence, offline, recovery, corruption, one-in-flight, duplicates, back-off, cache) | `../tests/lua/mem_tests.lua` on a **real Luau interpreter** with **simulated** Roblox/executor/Render stubs | 47/47 |
| Farm Additional tab / Auto Farm tab | `../tests/lua/ui_tests.lua`: the real UI code run against a **recording stand-in** for AdminUI | 15/15 |
| Static: compiles in real Luau (local/upvalue limits), unchanged regions, no Pastefy secrets in Lua, library methods exist | `../tests/static_checks.py` | 14/14 |
| Client ↔ server wire contract | `../tests/contract_test.py` (real Lua client code + real `server.py`) | 8/8 |
| **Real Pastefy API** | not run (no token); `tests/live_pastefy_smoke.py` provided | **untested** |
| **Real Render deployment** | not deployed | **untested** |
| **Roblox / an executor / real AdminUI rendering** | not run | **untested** |
Unverified assumptions about Pastefy: the exact GET JSON shape (both wrapped `{"paste":{…}}` and flat are accepted), that `PUT {"content":…}` alone updates content without touching other fields (read-back verification flags it if not), size limits, and the real rate limits.
