import sys, os, json, math, hashlib, tempfile, threading, sqlite3, http.client
sys.path.insert(0, os.path.dirname(__file__))
import server as S
R = []
def check(name, cond, detail=''):
    R.append((name, bool(cond))); print(('  PASS  ' if cond else '  FAIL  ') + name + (f'   [{detail}]' if detail and not cond else ''))
class Clock:
    def __init__(self): self.t = 1_700_000_000.0
    def __call__(self): return self.t
    def adv(self, s): self.t += s
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
def mk(path=None, min_sources=1, tokens=('tok-A', 'tok-B'), rate=120):
    clk = Clock(); path = path or tempfile.mktemp(suffix='.sqlite3')
    return S.NavService(path, [sha(t) for t in tokens], clock=clk, min_sources=min_sources, rate_per_min=rate), clk, path
SRC_A, SRC_B = sha('tok-A')[:16], sha('tok-B')[:16]
def ob(kind='stuck', x=10.0, z=10.0, outcome='fail', **kw):
    d = dict(v=1, map='123:Diner', sig='100x50x100', kind=kind, x=x, y=3.0, z=z, outcome=outcome, reason='stuck', nav='COLLECTING', recovery='replan', escape=False)
    d.update(kw); return d
def send(svc, obs, src=SRC_A): return svc.ingest(src, {'schema_version': 1, 'observations': obs if isinstance(obs, list) else [obs]})
def states(svc, map_id='123:Diner'):
    code, body = svc.advice(map_id); return {(r['kind'], r['x'], r['z']): r for r in body['records']} if code == 200 else None
def st(svc, kind='stuck', x=10.0, z=10.0):
    r = states(svc).get((kind, math.floor(x / 4) * 4 + 2, math.floor(z / 4) * 4 + 2)); return r['state'] if r else 'unknown'

print('== A. authentication, validation, versioning ==')
svc, clk, path = mk()
check('A1 no header -> 401; non-Bearer -> 401; wrong token -> 403; right token -> 200', [svc.authenticate(h)[0] for h in (None, 'Basic abc', 'Bearer nope', 'Bearer tok-A')] == [401, 401, 403, 200])
bad = [ob(kind='teleport'), ob(outcome='win'), ob(x=float('nan')), ob(x=float('inf')), ob(z=1e9), ob(reason="x'; DROP TABLE nav_records;--"), ob(reason='a' * 40),
       ob(map='../../etc/passwd'), ob(sig='<script>'), ob(escape='yes'), ob(v=2), {'v': 1}, 'string', ob(nav='ok; rm -rf')]
codes = [send(svc, [b])[0] for b in bad]
check('A2 every malformed / hostile observation is rejected with 422 (kind, outcome, NaN, inf, range, SQL-ish text, long text, path-ish map, markup, type, version)', all(c == 422 for c in codes), str(codes))
check('A3 nothing was stored by the rejected requests', svc.db.execute('SELECT COUNT(*) FROM nav_records').fetchone()[0] == 0)
check('A4 unsupported schema_version -> 422 and the supported list', svc.ingest(SRC_A, {'schema_version': 2, 'observations': [ob()]}) == (422, {'error': 'unsupported schema_version', 'supported': [1]}))
check('A5 batch limits: 0 and 51 observations rejected, 50 accepted', [send(svc, [])[0], send(svc, [ob(x=i * 8.0) for i in range(51)])[0], send(svc, [ob(x=i * 8.0, z=500.0) for i in range(50)])[0]] == [422, 422, 200])
check('A6 unknown extra fields are ignored (not stored) rather than trusted', send(svc, ob(x=900.0, evil='payload', player='someone'))[0] == 200 and 'evil' not in str(svc.db.execute('SELECT * FROM nav_observations').fetchall()))

print('\n== B. learning: evidence, expiry, contradiction ==')
svc, clk, _ = mk()
for i in range(2): send(svc, ob(outcome='fail'))
two = st(svc)
send(svc, ob(outcome='fail')); three = st(svc)
check('B1 two failures are not enough; three make the spot "unreliable"', two == 'unknown' and three == 'unreliable', f'{two},{three}')
svc, clk, _ = mk()
for i in range(2): send(svc, ob(kind='route', outcome='success'))
a = st(svc, 'route'); send(svc, ob(kind='route', outcome='success')); b = st(svc, 'route')
check('B2 two successes are not enough; three make a "candidate" (confidence >= 0.75)', a == 'unknown' and b == 'candidate' and states(svc)[('route', 10, 10)]['confidence'] >= 0.75, f'{a},{b}')
svc, clk, _ = mk()
for i in range(3): send(svc, ob(kind='coin', outcome='abandon', reason='no-route'))
u = st(svc, 'coin'); clk.adv(6 * 3600 + 60); expired = st(svc, 'coin')
check('B3 three abandons -> "unreachable"; after 6 h it expires so the spot is RETESTED (never blacklisted for ever)', u == 'unreachable' and expired == 'unknown', f'{u},{expired}')
svc, clk, _ = mk()
for i in range(3): send(svc, ob(outcome='fail'))
clk.adv(15 * 3600); mid = st(svc); clk.adv(10 * 3600); late = st(svc)
check('B4 "unreliable" holds for 24 h and then lapses', mid == 'unreliable' and late == 'unknown', f'{mid},{late}')
svc, clk, _ = mk()
for i in range(3): send(svc, ob(outcome='fail'))
clk.adv(28 * 86400)
check('B5 old evidence decays (two half-lives: 3 failures count as 0.75), independent of the TTL', svc.db.execute('SELECT failures FROM nav_records').fetchone()[0] == 3 and st(svc) == 'unknown')
svc, clk, _ = mk(); trail = []
for i in range(6): send(svc, ob(outcome='fail'))
trail.append(st(svc)); confs = []
for i in range(12):
    send(svc, ob(outcome='success')); trail.append(st(svc)); r = states(svc).get(('stuck', 10, 10)); confs.append(r['confidence'] if r else None)
cs = [c for c in confs if c is not None]
check('B6 contradiction: after 6 failures a single success already drops the "unreliable" verdict (it does not stick) without jumping to "candidate"; only sustained success re-earns it', trail[0] == 'unreliable' and trail[1] == 'unknown' and trail[-1] == 'candidate' and 'candidate' not in trail[:6], str(trail))
check('B6b the stored confidence rises monotonically while the contradicting successes accumulate', all(b >= a - 1e-9 for a, b in zip(cs, cs[1:])), str(cs))
svc, clk, _ = mk()
for i in range(4): send(svc, ob(kind='route', outcome='success'))
before = st(svc, 'route'); send(svc, ob(kind='route', outcome='fail', sig='300x50x300'))
check('B7 the map changed (different size signature): everything remembered is weakened to a quarter, so an old "candidate" is not trusted', before == 'candidate' and st(svc, 'route') == 'unknown')
svc, clk, _ = mk()
for i in range(4): send(svc, ob(kind='escape', outcome='success', escape=True))
c1 = st(svc, 'escape'); send(svc, ob(kind='escape', outcome='unsafe', reason='he followed'))
inv = st(svc, 'escape'); clk.adv(1900); after = st(svc, 'escape')
check('B8 an escape destination that stops being safe is invalidated at once, then lapses after 30 min WITHOUT being a candidate again (must be re-earned)', c1 == 'candidate' and inv == 'invalid' and after == 'unknown', f'{c1},{inv},{after}')
svc, clk, _ = mk(min_sources=2)
for i in range(3): send(svc, ob(outcome='fail'), SRC_A)
one = st(svc); send(svc, ob(outcome='fail'), SRC_B); two = st(svc)
check('B9 with NAV_MIN_SOURCES=2 a single contributor cannot establish a state; a second independent one can', one == 'unknown' and two == 'unreliable')

print('\n== C. versioning, persistence, privacy, no code ==')
svc, clk, path = mk()
for i in range(3): send(svc, ob(outcome='fail'))
svc.db.execute("INSERT INTO nav_records (schema_version,map_id,kind,cell_x,cell_z,failures,first_seen,last_seen,obs_count) VALUES (0,'123:Diner','stuck',50,50,9,?,?,9)", (int(clk()), int(clk())))
svc.db.commit(); S.MIN_SUPPORTED = 1
check('C1 rows written under an older schema (v0) are ignored by advice, current rows still served', ('stuck', 202, 202) not in states(svc) and ('stuck', 10, 10) in states(svc))
svc2 = S.NavService(path, [sha('tok-A')], clock=clk)
check('C2 reopening the same database is idempotent (migrations applied once) and keeps the data (persistent-disk case)', svc2.db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 1 and ('stuck', 10, 10) in states(svc2))
fresh, _, _ = mk()
check('C3 a brand-new database (ephemeral filesystem after a restart) is empty but fully working', states(fresh) == {} and send(fresh, ob(outcome='fail'))[0] == 200)
raw = open(path, 'rb').read() + (open(path + '-wal', 'rb').read() if os.path.exists(path + '-wal') else b'')
cols = {r[1] for t in ('nav_records', 'nav_observations', 'record_sources') for r in svc.db.execute(f'PRAGMA table_info({t})')}
check('C4 privacy: tokens are never stored (only a 16-hex hash), and no ip / user / player / name column exists', b'tok-A' not in raw and not any(w in c for c in cols for w in ('ip', 'user', 'player', 'name', 'account')), str(cols))
code, body = svc.advice('123:Diner'); WL = {'kind', 'x', 'y', 'z', 'state', 'confidence', 'observations', 'sources', 'last_seen', 'expires'}
ok = all(set(r) == WL and r['kind'] in S.KINDS and r['state'] in S.TTL and all(isinstance(r[k], (int, float)) for k in WL - {'kind', 'state'}) for r in body['records'])
check('C5 advice is data only: fixed keys, enum strings, numbers; no free text, no code', ok and set(body) == {'schema_version', 'server_time', 'map', 'records'} and 'function' not in json.dumps(body))

print('\n== D. HTTP layer (real sockets) ==')
svc, clk, path = mk(rate=5); srv = S.serve(svc, '127.0.0.1', 0); port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
def req(method, path_, body=None, tok='tok-A', headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5); h = dict(headers or {})
    if tok: h['Authorization'] = 'Bearer ' + tok
    c.request(method, path_, body=body, headers=h); r = c.getresponse(); data = r.read(); c.close()
    try: return r.status, json.loads(data)
    except Exception: return r.status, data
check('D1 /healthz needs no auth and reports the db (this is what UptimeRobot polls)', req('GET', '/healthz', tok=None)[0] == 200 and req('GET', '/healthz', tok=None)[1]['db'] == 'ok')
check('D2 advice without a token -> 401, wrong token -> 403', req('GET', '/v1/maps/x/advice', tok=None)[0] == 401 and req('GET', '/v1/maps/x/advice', tok='bad')[0] == 403)
check('D3 POST end to end: ingest then advice', req('POST', '/v1/observations', json.dumps({'schema_version': 1, 'observations': [ob()]}))[0] == 200 and req('GET', '/v1/maps/123%3ADiner/advice')[0] == 200)
check('D4 malformed JSON -> 400; oversized body -> 413; unknown route -> 404', req('POST', '/v1/observations', '{nope')[0] == 400 and req('POST', '/v1/observations', 'x' * 70000)[0] == 413 and req('GET', '/nope')[0] == 404)
r5 = [req('GET', '/v1/maps/x/advice')[0], req('GET', '/v1/maps/x/advice')[0]]
clk.adv(61)
check('D5 rate limit (5/min here): the 5th authenticated request passes, the 6th gets 429, and it recovers after the window', r5 == [200, 429] and req('GET', '/v1/maps/x/advice')[0] == 200, str(r5))
svc.db.close(); code = req('GET', '/healthz', tok=None)[0]
check('D6 database failure is visible to the monitor: /healthz -> 503', code == 503)
srv.shutdown()
print('\nsummary:', sum(1 for _, p in R if p), '/', len(R), 'passed'); json.dump([{'test': n, 'pass': p} for n, p in R], open(os.path.join(os.path.dirname(__file__), 'backend_last.json'), 'w'))
sys.exit(0 if all(p for _, p in R) else 1)
