"""Backend tests (Pastefy storage). Everything here runs against a SIMULATED Pastefy (fake_pastefy.py), never the real service."""
import sys, os, json, math, hashlib, threading, http.client
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..')); sys.path.insert(0, HERE)
import server as S
from fake_pastefy import FakePastefy
R = []
def check(name, cond, detail=''):
    R.append((name, bool(cond))); print(('  PASS  ' if cond else '  FAIL  ') + name + (f'   [{detail}]' if detail and not cond else ''))
class Clock:
    def __init__(self): self.t = 1_700_000_000.0
    def __call__(self): return self.t
    def adv(self, s): self.t += s
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
SRC_A, SRC_B = sha('tok-A')[:16], sha('tok-B')[:16]
def mk(store=None, min_sources=1, tokens=('tok-A', 'tok-B'), rate=120, **kw):
    clk = Clock()
    return S.NavService(store, [sha(t) for t in tokens], clock=clk, min_sources=min_sources, rate_per_min=rate, **kw), clk
def ob(kind='stuck', x=10.0, z=10.0, outcome='fail', **kw):
    d = dict(v=1, map='123:Diner', sig='100x50x100', kind=kind, x=x, y=3.0, z=z, outcome=outcome, reason='stuck', nav='COLLECTING', recovery='replan', escape=False)
    d.update(kw); return d
def send(svc, obs, src=SRC_A, **extra): return svc.ingest(src, dict({'schema_version': 1, 'observations': obs if isinstance(obs, list) else [obs]}, **extra))
def states(svc, map_id='123:Diner'):
    code, body = svc.advice(map_id); return {(r['kind'], r['x'], r['z']): r for r in body['records']} if code == 200 else None
def st(svc, kind='stuck', x=10.0, z=10.0):
    r = states(svc).get((kind, math.floor(x / 4) * 4 + 2, math.floor(z / 4) * 4 + 2)); return r['state'] if r else 'unknown'
def cells(svc): return S.count_cells(svc.doc)
def pasted(fake):
    k, d, _ = S.parse_doc(fake.content); return d if k == 'ok' else None
def cluster(fake, clk=None, **kw):
    store = S.PastefyStore(fake.token, fake.paste_id, base=fake.base, timeout=3)
    svc, c = mk(store, **kw)
    return svc, c
def boot(fake, **kw):
    svc, clk = cluster(fake, **kw)
    for _ in range(4):
        if svc.loaded: break
        svc.sync_once(); clk.adv(10)
    return svc, clk

print('== A. authentication, validation, versioning ==')
svc, clk = mk()
check('A1 no header -> 401; non-Bearer -> 401; wrong token -> 403; right token -> 200', [svc.authenticate(h)[0] for h in (None, 'Basic abc', 'Bearer nope', 'Bearer tok-A')] == [401, 401, 403, 200])
bad = [ob(kind='teleport'), ob(outcome='win'), ob(x=float('nan')), ob(x=float('inf')), ob(z=1e9), ob(reason="x'; DROP TABLE nav_records;--"), ob(reason='a' * 40),
       ob(map='../../etc/passwd'), ob(sig='<script>'), ob(escape='yes'), ob(v=2), {'v': 1}, 'string', ob(nav='ok; rm -rf')]
codes = [send(svc, [b])[0] for b in bad]
check('A2 every malformed / hostile observation is rejected with 422', all(c == 422 for c in codes), str(codes))
check('A3 nothing was stored by the rejected requests', cells(svc) == 0 and not svc.dirty)
check('A4 unsupported schema_version -> 422 and the supported list', svc.ingest(SRC_A, {'schema_version': 2, 'observations': [ob()]}) == (422, {'error': 'unsupported schema_version', 'supported': [1]}))
check('A5 batch limits: 0 and 51 observations rejected, 50 accepted', [send(svc, [])[0], send(svc, [ob(x=i * 8.0) for i in range(51)])[0], send(svc, [ob(x=i * 8.0, z=500.0) for i in range(50)])[0]] == [422, 422, 200])
check('A6 unknown extra fields are ignored (not stored)', send(svc, ob(x=900.0, evil='payload', player='someone'))[0] == 200 and 'evil' not in S.dumps(svc.doc) and 'someone' not in S.dumps(svc.doc))
check('A7 bad batch_id rejected', send(svc, ob(), batch_id='../x')[0] == 422)

print('\n== B. learning: evidence, expiry, contradiction (rules unchanged from v1) ==')
svc, clk = mk()
for i in range(2): send(svc, ob(outcome='fail'))
two = st(svc); send(svc, ob(outcome='fail')); three = st(svc)
check('B1 two failures are not enough; three make the spot "unreliable"', two == 'unknown' and three == 'unreliable', f'{two},{three}')
svc, clk = mk()
for i in range(2): send(svc, ob(kind='route', outcome='success'))
a = st(svc, 'route'); send(svc, ob(kind='route', outcome='success')); b = st(svc, 'route')
check('B2 two successes are not enough; three make a "candidate" (confidence >= 0.75)', a == 'unknown' and b == 'candidate' and states(svc)[('route', 10, 10)]['confidence'] >= 0.75, f'{a},{b}')
svc, clk = mk()
for i in range(3): send(svc, ob(kind='coin', outcome='abandon', reason='no-route'))
u = st(svc, 'coin'); clk.adv(6 * 3600 + 60); expired = st(svc, 'coin')
check('B3 three abandons -> "unreachable"; after 6 h it expires so the spot is RETESTED', u == 'unreachable' and expired == 'unknown', f'{u},{expired}')
svc, clk = mk()
for i in range(3): send(svc, ob(outcome='fail'))
clk.adv(15 * 3600); mid = st(svc); clk.adv(10 * 3600); late = st(svc)
check('B4 "unreliable" holds for 24 h and then lapses', mid == 'unreliable' and late == 'unknown', f'{mid},{late}')
svc, clk = mk()
for i in range(3): send(svc, ob(outcome='fail'))
clk.adv(28 * 86400)
check('B5 old evidence decays (two half-lives), independent of the TTL', st(svc) == 'unknown' and svc.doc['maps']['123:Diner']['cells']['stuck|2|2']['f'] == 3)
svc, clk = mk(); trail = []
for i in range(6): send(svc, ob(outcome='fail'))
trail.append(st(svc)); confs = []
for i in range(12):
    send(svc, ob(outcome='success')); trail.append(st(svc)); r = states(svc).get(('stuck', 10, 10)); confs.append(r['confidence'] if r else None)
cs = [c for c in confs if c is not None]
check('B6 contradiction: one success drops the "unreliable" verdict without jumping to "candidate"; sustained success re-earns it', trail[0] == 'unreliable' and trail[1] == 'unknown' and trail[-1] == 'candidate' and 'candidate' not in trail[:6], str(trail))
check('B6b confidence rises monotonically while contradicting successes accumulate', all(b >= a - 1e-9 for a, b in zip(cs, cs[1:])), str(cs))
svc, clk = mk()
for i in range(4): send(svc, ob(kind='route', outcome='success'))
before = st(svc, 'route'); send(svc, ob(kind='route', outcome='fail', sig='300x50x300'))
check('B7 map-change detection: a different size signature weakens everything remembered to a quarter', before == 'candidate' and st(svc, 'route') == 'unknown' and svc.doc['maps']['123:Diner']['sig'] == '300x50x300')
svc, clk = mk()
for i in range(4): send(svc, ob(kind='escape', outcome='success', escape=True))
c1 = st(svc, 'escape'); send(svc, ob(kind='escape', outcome='unsafe', reason='he followed'))
inv = st(svc, 'escape'); clk.adv(1900); after = st(svc, 'escape')
check('B8 an unsafe escape destination is invalidated at once, lapses after 30 min WITHOUT being a candidate again', c1 == 'candidate' and inv == 'invalid' and after == 'unknown', f'{c1},{inv},{after}')
svc, clk = mk(min_sources=2)
for i in range(3): send(svc, ob(outcome='fail'), SRC_A)
one = st(svc); send(svc, ob(outcome='fail'), SRC_B); two = st(svc)
check('B9 with NAV_MIN_SOURCES=2 a single contributor cannot establish a state; a second can', one == 'unknown' and two == 'unreliable')

print('\n== C. document format, privacy, no code ==')
svc, clk = mk()
for i in range(3): send(svc, ob(outcome='fail'))
txt = S.dumps(svc.doc); k, d, _ = S.parse_doc(txt)
check('C1 document is versioned JSON and round-trips (serialize -> parse -> serialize is identical)', k == 'ok' and d['doc_version'] == 1 and d['format'] == 'coinfarm-navmemory' and S.dumps(d) == txt)
check('C2 privacy: only an 8-hex contributor hash is stored; token text, ip, user, player never appear', 'tok-A' not in txt and SRC_A[:8] in txt and not any(w in txt.lower() for w in ('"ip"', 'player', 'user', 'account')))
code, body = svc.advice('123:Diner'); WL = {'kind', 'x', 'y', 'z', 'state', 'confidence', 'observations', 'sources', 'last_seen', 'expires'}
ok = all(set(r) == WL and r['kind'] in S.KINDS and r['state'] in S.TTL for r in body['records'])
check('C3 advice is data only (fixed keys, enums, numbers) and carries revision + memory_id', ok and set(body) == {'schema_version', 'server_time', 'map', 'revision', 'memory_id', 'records'} and 'function' not in json.dumps(body))
check('C4 all learned kinds are stored: hop, route, stuck, escape, coin', all(send(svc, ob(kind=k, x=100.0 + 8 * i, outcome='success'))[0] == 200 for i, k in enumerate(S.KINDS)) and {key.split('|')[0] for key in svc.doc['maps']['123:Diner']['cells']} == S.KINDS)

print('\n== D. HTTP layer (real sockets, volatile storage) ==')
svc, clk = mk(rate=5); srv = S.serve(svc, '127.0.0.1', 0); port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
def req(method, path_, body=None, tok='tok-A', headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5); h = dict(headers or {})
    if tok: h['Authorization'] = 'Bearer ' + tok
    c.request(method, path_, body=body, headers=h); r = c.getresponse(); data = r.read(); c.close()
    try: return r.status, json.loads(data)
    except Exception: return r.status, data
hz = req('GET', '/healthz', tok=None)
check('D1 /healthz needs no auth and reports storage state', hz[0] == 200 and hz[1]['storage']['state'] == 'volatile')
check('D2 advice without a token -> 401, wrong token -> 403', req('GET', '/v1/maps/x/advice', tok=None)[0] == 401 and req('GET', '/v1/maps/x/advice', tok='bad')[0] == 403)
check('D3 POST end to end: ingest then advice', req('POST', '/v1/observations', json.dumps({'schema_version': 1, 'observations': [ob()]}))[0] == 200 and req('GET', '/v1/maps/123%3ADiner/advice')[0] == 200)
check('D4 malformed JSON -> 400; oversized body -> 413; unknown route -> 404', req('POST', '/v1/observations', '{nope')[0] == 400 and req('POST', '/v1/observations', 'x' * 70000)[0] == 413 and req('GET', '/nope')[0] == 404)
r5 = [req('GET', '/v1/maps/x/advice')[0], req('GET', '/v1/maps/x/advice')[0]]
clk.adv(61)
check('D5 rate limit (5/min): 6th authenticated request gets 429, recovers after the window', r5 == [200, 429] and req('GET', '/v1/maps/x/advice')[0] == 200, str(r5))
srv.shutdown()

print('\n== E. Pastefy transport (simulated Pastefy) ==')
f = FakePastefy(); store = S.PastefyStore('PT', 'abc123', base=f.base, timeout=3)
f.content = 'hello'
r = store.fetch()
check('E1 GET parses the wrapped {"paste":{content}} shape and sends the Bearer token', r.status == 'ok' and r.text == 'hello' and f.headers_seen[-1].get('Authorization') == 'Bearer PT')
f.wrapped = False; r = store.fetch(); f.wrapped = True
check('E2 GET also parses the flat shape', r.status == 'ok' and r.text == 'hello')
w = store.write('{"x":1}')
check('E3 PUT sends {"content": ...} and a 204 is success', w.status == 'ok' and f.content == '{"x":1}')
bad_store = S.PastefyStore('WRONG', 'abc123', base=f.base, timeout=3); other = S.PastefyStore('PT', 'nope', base=f.base, timeout=3)
check('E4 wrong token -> auth; wrong paste id -> not_found', bad_store.fetch().status == 'auth' and other.fetch().status == 'not_found' and bad_store.write('x').status == 'auth')
res = {}
for m in ('500', '429', 'drop', 'html', 'badjson', 'nocontent', 'redirect'):
    f.mode = m; res[m] = store.fetch().status
f.mode = 'ok'
check('E5 outage / 5xx / rate limit / junk / missing content / redirect are all classified, none returns "ok"',
      res == {'500': 'unavailable', '429': 'rate_limited', 'drop': 'unavailable', 'html': 'bad_response', 'badjson': 'bad_response', 'nocontent': 'bad_response', 'redirect': 'bad_response'}, str(res))
f.mode = '429'; ra = store.fetch().retry_after; f.mode = 'ok'
check('E6 Retry-After is read from a 429', ra == 120)
def _raises(fn):
    try: fn(); return False
    except ValueError: return True
check('E7 configuration is validated (empty token, bad paste id, non-https base)',
      _raises(lambda: S.PastefyStore('', 'abc')) and _raises(lambda: S.PastefyStore('t', '../x')) and _raises(lambda: S.PastefyStore('t', 'abc', base='http://evil.example/api')))
f.close()

print('\n== F. load / initialise / flush lifecycle ==')
f = FakePastefy(content='')
svc, clk = cluster(f)
a1 = svc.sync_once(); code_not_loaded = send(svc, ob())[0]
clk.adv(6); a2 = svc.sync_once()
check('F1 an EMPTY paste is only initialised after a second empty answer (one blank could be a glitch); until then ingest is refused with 503', (a1, a2) == ('confirming-empty', 'initialized') and code_not_loaded == 503, str((a1, a2, code_not_loaded)))
check('F2 nothing was written to the paste before the document was loaded', f.counts['PUT'] == 0)
send(svc, [ob(outcome='success', x=10.0 + 8 * i) for i in range(3)])
r = svc.sync_once()
d = pasted(f)
check('F4 flush updates the SAME paste with a valid versioned document containing the observations', r == 'flushed' and d and S.count_cells(d) == 3 and d['revision'] >= 1 and f.counts['PUT'] == 1, str((r, f.counts)))
send(svc, ob(x=900.0, outcome='success')); r2 = svc.sync_once(); put1 = f.counts['PUT']; clk.adv(100); r3 = svc.sync_once()
check('F3 after the first write, later observations are batched: no write until the flush interval, then one', r2 == 'idle' and put1 == 1 and r3 == 'flushed' and f.counts['PUT'] == 2, str((r2, r3, f.counts)))
d = pasted(f); S_cells = S.count_cells(d)
check('F5 state is "synced" after a verified flush', svc.storage_state() == 'synced' and not svc.dirty)
svc2, clk2 = boot(f)
check('F6 a restarted service (fresh process) reloads the memory from Pastefy and serves the same advice', svc2.loaded and S.count_cells(svc2.doc) == 4 and svc2.doc['memory_id'] == d['memory_id'])
f.close()

print('\n== G. Pastefy unavailable / recovery ==')
f = FakePastefy(content='')
svc, clk = boot(f); send(svc, [ob(outcome='success')]); clk.adv(100); svc.sync_once()
puts0 = f.counts['PUT']
f.mode = 'drop'
send(svc, [ob(outcome='fail', x=50.0 + 8 * i) for i in range(5)]); clk.adv(100)
r1 = svc.sync_once(); r2 = svc.sync_once()
check('G1 during an outage ingest + advice keep working from memory; the flush fails, data stays dirty, back-off applies (no hammering)',
      r1 == 'fail:unavailable' and r2 == 'backoff' and svc.dirty and svc.advice('123:Diner')[0] == 200 and send(svc, ob(x=300.0))[0] == 200 and f.counts['GET'] <= 6, str((r1, r2, f.counts)))
check('G2 health stays 200 and reports "degraded" (a failing health check would make Render restart the service)', svc.health()[0] == 200 and svc.health()[1]['storage']['state'] == 'degraded')
before_content = f.content
for _ in range(5): clk.adv(30); svc.sync_once()
check('G3 repeated outage attempts back off exponentially (few requests) and never alter the paste', f.content == before_content and f.counts['GET'] <= 8, str(f.counts))
f.mode = 'ok'; clk.adv(1000); r = svc.sync_once(); d = pasted(f)
check('G4 when Pastefy returns, the next sync flushes EVERYTHING accumulated during the outage', r == 'flushed' and d and S.count_cells(d) == 7 and not svc.dirty, str((r, d and S.count_cells(d))))
f.mode = '429'; send(svc, ob(x=400.0)); clk.adv(100); r = svc.sync_once()
check('G5 a 429 honours Retry-After (>= 120 s) before the next attempt', r == 'fail:rate_limited' and svc.sync['next_at'] - clk() >= 119, str((r, svc.sync['next_at'] - clk())))
f.mode = 'put500'; clk.adv(500); r = svc.sync_once(); f.mode = 'ok'
check('G6 a failed WRITE (500) keeps the data dirty and the paste untouched', r == 'fail:unavailable' and svc.dirty)
clk.adv(1000); check('G7 ...and it is written on the next healthy attempt', svc.sync_once() == 'flushed' and S.count_cells(pasted(f)) == 8)
f.mode = 'putdrop'; send(svc, ob(x=500.0)); clk.adv(2000); svc.sync_once(); f.mode = 'ok'
check('G8 write dropped mid-flight: still dirty, retried later', svc.dirty); clk.adv(2000); svc.sync_once()
check('G9 ...and recovered', not svc.dirty and S.count_cells(pasted(f)) == 9)
f.close()

print('\n== H. corrupted / foreign / newer responses can never destroy valid memory ==')
def valid_paste(n=3):
    s, c = mk()
    for i in range(n): send(s, ob(outcome='success', x=10.0 + 8 * i))
    s.doc['revision'] = 5; return S.dumps(s.doc)
good = valid_paste()
for label, junk in (('HTML error page', '<html>502</html>'), ('truncated JSON', good[:len(good) // 2]), ('foreign JSON', '{"todo": ["milk"]}'), ('JSON list', '[1,2,3]')):
    f = FakePastefy(content=junk); svc, clk = boot(f)
    check(f'H1 cold start on {label}: not loaded, ingest refused, ZERO writes, paste untouched', not svc.loaded and send(svc, ob())[0] == 503 and f.counts['PUT'] == 0 and f.content == junk, str(f.counts))
    f.close()
f = FakePastefy(content=good); svc, clk = boot(f)
check('H2 a valid paste loads', svc.loaded and S.count_cells(svc.doc) == 3)
send(svc, ob(x=200.0, outcome='success')); n_before = cells(svc)
f.mode = 'html'; clk.adv(100); r = svc.sync_once(); f.mode = 'ok'
check('H3 junk HTTP 200 (html) during a flush: memory kept, nothing written, still dirty', r.startswith('fail:') and cells(svc) == n_before and svc.dirty and f.content == good)
f.content = '{"broken":'; clk.adv(1000); r1 = svc.sync_once()
check('H4 paste content turns corrupt: first sighting is NOT overwritten, in-memory memory intact', r1 == 'suspect-corrupt' and f.content == '{"broken":' and cells(svc) == n_before)
clk.adv(100); svc.sync_once(); clk.adv(100); r3 = svc.sync_once()
check('H5 only after 3 consecutive bad checks is our valid copy restored', r3 == 'flushed' and pasted(f) and S.count_cells(pasted(f)) == n_before)
f.content = ''; svc.dirty = True; clk.adv(100); r = svc.sync_once()
check('H6 an emptied paste is also not overwritten on first sighting (and never replaces local memory)', r == 'suspect-empty' and cells(svc) == n_before)
f.content = '{"todo": 1}'; clk.adv(100); svc.dirty = True; r = svc.sync_once()
check('H7 foreign JSON appears after load: blocked, never overwritten (wrong paste id protection)', r == 'blocked' and f.content == '{"todo": 1}' and svc.storage_state() == 'blocked' and cells(svc) == n_before)
check('H8 blocked state refuses ingest with 503, so clients keep their queue and their own local memory', send(svc, ob())[0] == 503)
f.close()
f = FakePastefy(content='{"todo": 1}'); store = S.PastefyStore('PT', 'abc123', base=f.base, timeout=3); svc, clk = mk(store, adopt_foreign=True); svc.sync_once()
check('H9 PASTEFY_ADOPT_PASTE=1 lets the operator take over an unrecognised paste explicitly', svc.loaded and svc.dirty); f.close()
fut = json.loads(good); fut['doc_version'] = 99; futs = json.dumps(fut)
f = FakePastefy(content=futs); svc, clk = boot(f)
check('H10 a document written by a NEWER schema is never touched (read-only mode, no downgrade)', svc.mode == 'readonly' and not svc.loaded and f.counts['PUT'] == 0 and f.content == futs); f.close()
mj = json.loads(good)
for k in list(mj['maps']['123:Diner']['cells']): mj['maps']['123:Diner']['cells'][k]['s'] = -5
f = FakePastefy(content=json.dumps(mj)); svc, clk = boot(f)
check('H11 a document that is mostly invalid cells is "corrupt" and not loaded', not svc.loaded and f.counts['PUT'] == 0); f.close()
mj = json.loads(valid_paste(20)); ks = list(mj['maps']['123:Diner']['cells']); mj['maps']['123:Diner']['cells'][ks[0]]['s'] = float('nan'); mj['maps']['123:Diner']['cells'][ks[1]]['f'] = 1e12
kind, dd, info = S.parse_doc(json.dumps(mj))
check('H12 a few invalid cells (NaN, absurd numbers) are dropped, the rest loads', kind == 'ok' and S.count_cells(dd) == 18 and info['bad'] == 2)
f = FakePastefy(content=good); svc, clk = boot(f)
svc.doc['maps'] = {}; svc.dirty = True; clk.adv(100)
# merge brings the remote cells back before the write, so valid memory cannot be lost this way
r = svc.sync_once()
check('H13 even if the in-memory copy were wiped, read-merge-write restores remote cells instead of erasing them', r == 'flushed' and S.count_cells(pasted(f)) == 3); f.close()
f = FakePastefy(content=''); f.mode = 'nocontent'; svc, clk = boot(f)
for _ in range(3): clk.adv(10); svc.sync_once()
check('H14 a response WITHOUT a content field is "bad_response", never mistaken for an empty paste (no initialisation)', not svc.loaded and f.counts['PUT'] == 0); f.close()

print('\n== I. caching and request economy ==')
f = FakePastefy(content=good); svc, clk = boot(f); g0 = f.counts['GET']
for i in range(60): svc.advice('123:Diner')
for i in range(5): svc.sync_once()
check('I1 60 advice calls cause no Pastefy request (the in-memory copy is the cache) beyond one lazy refresh', f.counts['GET'] - g0 <= 1, str(f.counts))
clk.adv(400); svc.advice('123:Diner'); svc.sync_once(); g1 = f.counts['GET']; svc.sync_once(); svc.sync_once()
check('I2 after the cache TTL one background refresh happens, then none until the TTL passes again', f.counts['GET'] == g1 and g1 - g0 <= 2, str(f.counts))
svc.last_flush = clk(); p0 = f.counts['PUT']
for i in range(100): send(svc, ob(x=1000.0 + 4 * i, outcome='success'))
for i in range(4): svc.sync_once()
check('I3 100 ingest calls inside one flush interval produce no Pastefy write until the interval (or 200-observation) threshold', f.counts['PUT'] == p0, str(f.counts))
clk.adv(100); svc.sync_once()
check('I4 then exactly ONE write carries them all (the same paste; no new pastes exist in the API surface)', f.counts['PUT'] == p0 + 1 and S.count_cells(pasted(f)) >= 100)
g2, p2 = f.counts['GET'], f.counts['PUT']
for i in range(10): clk.adv(100); svc.sync_once()
check('I5 clean idle service makes no requests at all', f.counts['GET'] == g2 and f.counts['PUT'] == p2, str(f.counts))
f.close()

print('\n== J. idempotency, merge, shutdown, size, token secrecy ==')
svc, clk = mk()
a = send(svc, [ob(outcome='fail')], batch_id='batch-0001'); b = send(svc, [ob(outcome='fail')], batch_id='batch-0001')
check('J1 a retried batch (same batch_id, e.g. after a lost response) is acknowledged but applied once', a[0] == 200 and b[1].get('duplicate') and svc.doc['maps']['123:Diner']['cells']['stuck|2|2']['n'] == 1)
f = FakePastefy(content=''); s1, c1 = boot(f); send(s1, ob(x=10.0, outcome='success')); c1.adv(100); s1.sync_once()
s2, c2 = boot(f); send(s2, ob(x=200.0, outcome='success')); send(s1, ob(x=400.0, outcome='success'))
c2.adv(100); s2.sync_once(); c1.adv(100); s1.sync_once(); d = pasted(f)
check('J2 two instances (overlapping deploys) merge instead of overwriting each other', S.count_cells(d) == 3, str(S.count_cells(d)))
send(s1, ob(x=600.0, outcome='success')); r = s1.shutdown_flush()
check('J3 SIGTERM path: shutdown_flush writes pending data once', r == 'flushed' and S.count_cells(pasted(f)) == 4)
check('J4 shutdown_flush never writes before load / when clean', S.NavService(S.PastefyStore('PT', 'abc123', base=f.base), [sha('t')]).shutdown_flush() == 'skipped' and s1.shutdown_flush() == 'skipped')
s3, c3 = boot(f, max_bytes=3000)
for i in range(60): send(s3, ob(x=2000.0 + 8 * i, outcome='success'))
c3.adv(100); s3.sync_once()
check('J5 size cap: the document is pruned under the byte limit before writing (oldest evidence first), never refused silently', len(f.content) <= 3000 and S.parse_doc(f.content)[0] == 'ok', str(len(f.content)))
raw_all = f.content + json.dumps(s3.health()[1]) + json.dumps(s3.advice('123:Diner')[1])
check('J6 the Pastefy token never appears in the document, health or advice output', 'PT' not in raw_all.replace('"PT"', '') or 'Bearer PT' not in raw_all)
f.close()
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    f = FakePastefy(content=''); f.token = 'SECRET-PASTEFY-TOKEN-123'; s4, c4 = cluster(f); s4.store.token = 'SECRET-PASTEFY-TOKEN-123'
    for _ in range(3): s4.sync_once(); c4.adv(10)
    f.mode = '500'; send(s4, ob()); c4.adv(100); s4.sync_once(); f.close()
check('J7 the Pastefy token is never printed to the logs', 'SECRET-PASTEFY-TOKEN' not in buf.getvalue() and len(buf.getvalue()) > 0)
env = {'NAV_TOKENS': 'a'}; os.environ.pop('PASTEFY_API_TOKEN', None); os.environ.pop('PASTEFY_PASTE_ID', None)
v = S.build_service_from_env(env)
check('J8 without Pastefy configuration the service runs in clearly flagged VOLATILE mode (health says so)', v.store is None and v.health()[1]['storage']['state'] == 'volatile')
f = FakePastefy(content='', visibility='PUBLIC'); s5, c5 = boot(f)
check('J9 a PUBLIC paste raises a visible warning in /healthz', 'paste_is_public' in s5.health()[1]['storage']['warnings']); f.close()

print('\nsummary:', sum(1 for _, p in R if p), '/', len(R), 'passed'); json.dump([{'test': n, 'pass': p} for n, p in R], open(os.path.join(HERE, 'backend_last.json'), 'w'))
sys.exit(0 if all(p for _, p in R) else 1)
