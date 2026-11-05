#!/usr/bin/env python3
"""Static checks on the delivered Lua script (no Roblox needed). Env: LUAU_COMPILE=/path/to/luau-compile, ORIG=original script, ORIG_UI=original library."""
import difflib, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
NEW = os.environ.get("COINFARM_LUA", os.path.join(HERE, "..", "CoinFarm_v9_hybridfix_4.lua"))
ORIG = os.environ.get("ORIG", os.path.join(HERE, "..", "..", "CoinFarm_v9_hybridfix_3.orig.lua"))
UI = os.environ.get("ORIG_UI", os.path.join(HERE, "..", "..", "AdminUI_Library_3.orig.lua"))
COMPILE = os.environ.get("LUAU_COMPILE", "luau-compile")
new, old, ui = (open(p, encoding="utf-8").read() for p in (NEW, ORIG, UI))
R = []
def check(n, c, d=""):
    R.append(bool(c)); print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))
print("== S. static checks ==")
for label, path in (("script", NEW), ("original script", ORIG), ("AdminUI library", UI)):
    r = subprocess.run([COMPILE, "--text", path], capture_output=True, text=True)
    check(f"S1 {label} compiles with the real Luau compiler (covers local-variable / upvalue / register limits)", r.returncode == 0, r.stderr[:200])
# which lines changed
a, b = old.split("\n"), new.split("\n")
sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
changed = [(t, i1 + 1, i2, j1 + 1, j2) for t, i1, i2, j1, j2 in sm.get_opcodes() if t != "equal"]
def region(i1, i2):
    txt = "\n".join(a[i1 - 1:i2])
    return txt
mem_a = old.index("CoinFarm.mem = { V = 1"); mem_a_line = old[:mem_a].count("\n") + 1
mem_b_line = old[:old.index("HYBRID (internal key")].count("\n") - 1
allowed = []
for t, i1, i2, j1, j2 in changed:
    ok = False
    if mem_a_line <= i1 <= mem_b_line + 3 and i2 <= mem_b_line + 3: ok = "mem"
    seg = "\n".join(a[i1 - 1:i2]) + "\n" + "\n".join(b[j1 - 1:j2])
    if any(k in seg for k in ("Die at Full Bag", "After bag full", "Memory server URL", "Farm Additional", "MemUrl", "mem.shutdown", "mem.save", "MM2 Coin Farm  -  v9", "Tab: ESP")): ok = ok or "ui"
    allowed.append(ok)
check("S2 every changed region of the original is inside the memory block, the relocated UI rows, the settings default, the header or the unload line", all(allowed), str([c for c, ok in zip(changed, allowed) if not ok]))
outside = sum((i2 - i1 + 1) for (t, i1, i2, j1, j2), ok in zip(changed, allowed) if ok != "mem")
print(f"        (regions changed outside the memory block: {len([1 for ok in allowed if ok == 'ui'])}, {outside} original lines)")
core = ["Legit.ev = {", "CoinFarm.post", "function Legit.evadeReset", "Threat", "CoinFarm.analysis", "THREAT_DETECTED", "SAFE_REASSESSMENT", "round", "respawn"]
_unl = "    pcall(CoinFarm.mem.shutdown) -- discards any in-flight sync answer, then writes the local memory once"
_core_new = new[new.index("HYBRID (internal key"):new.index("-- ---------- Tab: Auto Farm")].replace(_unl, "    pcall(CoinFarm.mem.save)")
check("S3 the hybrid escape / threat / jump / post-collection / round-change code is byte-identical to the uploaded file (only the unload line differs: save -> shutdown)",
      old[old.index("HYBRID (internal key"):old.index("-- ---------- Tab: Auto Farm")] == _core_new)
pre = old[:old.index("CoinFarm.mem = { V = 1")]
check("S4 everything before the memory block is identical except the settings default and header line", len([l for l in difflib.unified_diff(pre.split("\n"), new[:new.index("-- MEM-BEGIN")].split("\n"), lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]) <= 6)
mem = new[new.index("-- MEM-BEGIN"):new.index("-- MEM-END")]
check("S5 no Pastefy URL, token or API call anywhere in the Lua script", not re.search(r"pastefy\.app|PASTEFY_API|api/v2", new, re.I))
check("S6 memory block: no event connections (nothing to leak or duplicate) and exactly one task.spawn (one request in flight)", mem.count(":Connect(") == 0 and mem.count("task.spawn(") == 1)
tabblk = new[new.index("-- ---------- Tab: Farm Additional"):new.index("-- ---------- Tab: ESP")]
check("S7 the new tab contains no remote calls, tools, humanoid access or other-player targeting", not re.search(r"FireServer|InvokeServer|Humanoid|Tool|Players|Fling\(|Kill\(|Shoot\(|Backpack", tabblk))
check("S8 the locked toggles are still locked via Lock('disabled') and nothing unlocks them", tabblk.count('h.Lock("disabled")') == 1 and "Unlock" not in tabblk)
tabs = len(re.findall(r"win:AddTab\(", new))
check("S9 tab count (%d, one of them conditional) fits the library's tab list: %d x 40 px <= 250 px" % (tabs, tabs), tabs * 40 <= 250)
need = ["function AdminUI:AddTab", "function Tab:AddSection", "function Tab:AddToggle", "function Tab:AddSegmented", "e.Lock = function"]
check("S10 every library method the new tab uses exists in the uploaded AdminUI_Library (so the library needs no change)", all(n in ui for n in need))
check("S11 default memory URL is the existing Render deployment, token is not hardcoded", 'MemUrl = "https://memoryhades.onrender.com", MemToken = ""' in new)
check("S12 the DieAtFullBag / KillAll / ShootMurd / FlingMurd settings keys and defaults are unchanged", all(k in new for k in ("DieAtFullBag = false", "KillAllOnFullBag = false", "ShootMurdOnFullBag = false", "FlingMurdOnFullBag = false", "PostActions = \"record\"")))
print("\nstatic summary: %d / %d passed" % (sum(R), len(R))); sys.exit(0 if all(R) else 1)
