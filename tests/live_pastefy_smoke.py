#!/usr/bin/env python3
"""OPTIONAL live check against the REAL Pastefy API. NOT run by the author (no token available there): run it yourself.
  PASTEFY_API_TOKEN=... PASTEFY_PASTE_ID=... python tests/live_pastefy_smoke.py [--write]
Read-only unless --write; --write PUTs back exactly what it just read (a round-trip that proves PUT {"content"} works for your paste)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server as S
tok, pid = os.environ.get("PASTEFY_API_TOKEN", ""), os.environ.get("PASTEFY_PASTE_ID", "")
if not tok or not pid: sys.exit("set PASTEFY_API_TOKEN and PASTEFY_PASTE_ID")
st = S.PastefyStore(tok, pid, base=os.environ.get("PASTEFY_API_BASE", "https://pastefy.app/api/v2"))
r = st.fetch(); print("fetch:", r.status, "| visibility:", r.meta.get("visibility"), "| bytes:", len(r.text or ""))
if r.status != "ok": sys.exit(1)
kind, doc, info = S.parse_doc(r.text); print("content is:", kind, info)
if kind == "ok": print("revision", doc["revision"], "| cells", S.count_cells(doc), "| memory_id", doc["memory_id"])
if "--write" in sys.argv:
    w = st.write(r.text); print("write:", w.status)
    r2 = st.fetch(); print("read-back identical:", r2.status == "ok" and r2.text == r.text)
