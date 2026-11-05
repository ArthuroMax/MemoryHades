#!/usr/bin/env python3
"""Assemble and run the Lua test suites with a real Luau interpreter (env LUAU=/path/to/luau, default `luau` on PATH).
These tests run against SIMULATED Roblox/executor/Render APIs (stubs.lua). They are not Roblox tests."""
import os, re, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("COINFARM_LUA", os.path.join(HERE, "..", "..", "CoinFarm_v9_hybridfix_4.lua"))
LUAU = os.environ.get("LUAU", "luau")
src = open(SCRIPT, encoding="utf-8").read()
stubs = open(os.path.join(HERE, "stubs.lua")).read()
def between(a, b):
    i = src.index(a); j = src.index(b, i); return src[i:j + len(b)]
def run(name, chunk):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name + ".lua"); open(p, "w").write(chunk)
        r = subprocess.run([LUAU, p], capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode == 0
ok = True
mem = between("-- MEM-BEGIN", "-- MEM-END")
body = open(os.path.join(HERE, "mem_tests.lua")).read().replace("--MEM_BLOCK--", mem)
ok &= run("mem", stubs + "\n" + body)
ui = open(os.path.join(HERE, "ui_tests.lua")).read()
ui_block = between("-- ---------- Tab: Auto Farm ----------", "-- ---------- Tab: ESP ----------")
ok &= run("ui", stubs + "\n" + ui.replace("--UI_BLOCK--", ui_block))
sys.exit(0 if ok else 1)
