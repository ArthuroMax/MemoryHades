# Navigation-memory service (reference implementation)

Status: **implemented and tested locally (SQLite). Not deployed.** Python standard library only, no dependencies.
Sections: 1 limits of Render / UptimeRobot, 2 architecture, 3 API, 4 schema, 5 learning rules, 6 security + privacy, 7 deploying, 8 what is NOT done.

## 1. What Render and UptimeRobot can and cannot do  (checked against their documentation, Oct 2026 - re-check before relying on it)

**Render, free web service**
- Spins down after **15 minutes without inbound traffic**; the next request waits for a cold start (tens of seconds up to about a minute).
- The **local filesystem is ephemeral**: anything written to it - including a SQLite file - is lost on spin-down, restart and redeploy.
  Render may also restart a free service at any time.
- 750 free instance-hours per workspace per month (one always-on service uses about 720-744 of them).
- **No persistent disk** on free instances (disks attach to paid web services only, billed per GB-month).

**Render Postgres, free**: expires **30 days after creation** (short grace period, then it is gone). Paid Postgres is the durable option.

**UptimeRobot, free plan**: 50 monitors, 5-minute interval (check the current terms for commercial-use wording). It **monitors and alerts**; it cannot make a service
available. A 5-minute HTTP check does generate inbound traffic, so it will in practice keep a free Render service from spinning down - but that
spends the instance-hours, does not stop restarts, and does not make a SQLite file durable. Treat it as a **monitor**, not as a keep-alive guarantee.

**Consequence for the design**
1. The script must work with no server at all. It does: memory is local first (a file written by the executor) and the server is optional.
2. A free Render web service + SQLite loses everything on each restart. Do not use that combination for data you want to keep.
3. Durable options (pick one):
   - **A. Paid Render web service + persistent disk** with `NAV_DB_PATH=/var/data/nav.sqlite3` - works with the code exactly as written (single instance only).
   - **B. Render web service + a managed Postgres** (paid Render Postgres, or an external provider). Needs a Postgres storage adapter, **which is not written**
     (`schema.postgres.sql` is the target schema).
   - **C. Free service as a best-effort sync target only**: tolerate data loss; the clients re-upload their local memory on the next sync anyway.

## 2. Architecture

```
 Roblox executor (script)                          Render web service                      UptimeRobot
 ┌──────────────────────────┐   HTTPS + Bearer     ┌─────────────────────────┐   GET /healthz   ┌───────────┐
 │ CoinFarm.mem (local first)│ ── POST observations ▶ │ server.py (validate,    │ ◀─────────────── │ 5-min HTTP │
 │  - decayed counts per cell│ ◀─ GET advice ────── │  aggregate, advise)      │   200 / 503      │ monitor    │
 │  - JSON file via writefile│                      │  SQLite on persistent disk│ ───── alert ───▶ └───────────┘
 │  - offline queue, backoff │                      └─────────────────────────┘
 └──────────────────────────┘
```
- The client never blocks navigation on the network: one request in flight, 20 s watchdog, exponential back-off (30 s .. 10 min), queue capped at 200.
- The server never sends code. Advice is a fixed-shape list of numbers and enum strings; the client re-validates every field and ignores anything else.

## 3. API  (all JSON, `schema_version` = 1)
| | |
|---|---|
| `GET /healthz` | no auth; `200 {"ok":true,"schema":1,"db":"ok"}` or `503` - this is what UptimeRobot polls |
| `POST /v1/observations` | `Authorization: Bearer <token>`; `{"schema_version":1,"observations":[1..50 x obs]}` -> `{"accepted":n}`; 400 malformed, 401/403 auth, 413 > 64 KiB, 422 validation / unsupported version, 429 rate limit |
| `GET /v1/maps/<map_id>/advice` | Bearer; `{"schema_version":1,"server_time":..,"map":..,"records":[{kind,x,y,z,state,confidence,observations,sources,last_seen,expires}]}` (max 500) |

Observation: `v, map, sig, kind(hop|route|stuck|escape|coin), x,y,z, outcome(success|fail|abandon|unsafe), reason, nav, recovery, escape(bool), t` -
map id and size signature, the 4-stud cell, traversal class, outcome and reason, navigation state, recovery action attempted, whether an escape destination was reached, timestamp.
Strings are limited to `[A-Za-z0-9 _.\-:()]{0,32}`; coordinates to +-5000; non-finite numbers are rejected; unknown fields are ignored and not stored.

## 4. Schema (SQLite as implemented; Postgres equivalent in `schema.postgres.sql`)
`schema_migrations(version, applied_at)` - applied in order, idempotent, never edited after release.
`nav_records(schema_version, map_id, kind, cell_x, cell_z, y, successes, failures, abandons, unsafe, obs_count, last_reason, map_sig, first_seen, last_seen, last_unsafe)` unique per (map, kind, cell).
`record_sources(record_id, source_hash)` - distinct contributors (first 16 hex of SHA-256 of the token). `nav_observations` - bounded raw log (20 000 rows).
Every row carries `schema_version`; advice ignores rows below `MIN_SUPPORTED`, so a future script/schema can coexist with old rows until a migration upgrades them.

## 5. Learning rules (identical in `server.py` and the Lua `Mem.stateOf`)
Per record: counts decay with a 14-day half-life; a **contradicting** observation halves the opposing history; a changed **map signature** multiplies everything by 0.25.
| state | needs | lasts |
|---|---|---|
| `unreachable` | >= 2.5 (decayed) abandons, ~no success | 6 h, then the spot is **retested** |
| `unreliable` | >= 2.5 failures and confidence <= 0.3 | 24 h, then retested |
| `candidate` | >= 2.5 successes and confidence >= 0.75 | 7 days |
| `invalid` (escape destination) | any `unsafe` | 30 min, then it must **re-earn** trust |
Confidence = (s+1)/(s+f+2). `NAV_MIN_SOURCES` (default 1) can require several independent tokens before a state is advised.
Nothing is a permanent blacklist and nothing is "last result wins".

## 6. Security and privacy
Bearer tokens over HTTPS; only SHA-256 hashes are configured; constant-time comparison; per-token rate limit (120/min); 64 KiB body and 50-item batch limits;
the service refuses to start without a token. No IP addresses (request logging is off), no player ids, names or accounts are read or stored.
A token embedded in a client script can be extracted by whoever has the script: it keeps strangers out, it does not make an untrusted client trustworthy -
hence validation, ranges, rate limits and (for a public deployment) `NAV_MIN_SOURCES >= 2`.

## 7. Deploying (option A)
1. Create a Render **web service** (paid instance) from this folder, runtime Python, start command `python server.py`, health check path `/healthz`.
2. Attach a **persistent disk** mounted at `/var/data`; set `NAV_DB_PATH=/var/data/nav.sqlite3`.
3. Set `NAV_TOKENS` (comma separated secrets; generate with `python -c "import secrets;print(secrets.token_urlsafe(32))"`). Keep to **one instance** (SQLite + one disk).
4. UptimeRobot: HTTP(s) monitor on `https://<service>.onrender.com/healthz`, 5-minute interval, alert contact of your choice.
5. In the script's Farm tab set *Memory server URL* and *Memory token*. Empty = local memory only.
`render.yaml` is an **untested template** for the above - verify it against Render's Blueprint documentation.

## 8. Not done
Postgres adapter; deployment (no account / network access here); load testing; token rotation tooling; an admin view of the data.
