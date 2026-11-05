#!/bin/sh
# Runs every automated check. Needs python3 and a Luau build (https://github.com/luau-lang/luau: `luau` and `luau-compile`).
# usage: LUAU=/path/luau LUAU_COMPILE=/path/luau-compile ./run_all.sh
set -e
cd "$(dirname "$0")"
python3 -I ../backend/tests/test_backend.py | tail -1
LUAU="${LUAU:-luau}" python3 lua/run_lua_tests.py | grep summary
LUAU_COMPILE="${LUAU_COMPILE:-luau-compile}" python3 static_checks.py | tail -1
LUAU="${LUAU:-luau}" python3 contract_test.py | tail -1
