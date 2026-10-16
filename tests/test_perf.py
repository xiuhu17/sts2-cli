"""Performance features must not change what the engine says: protocol details, the
setup-time UI guard, the fast map pruning, engine selection and relocatable builds."""
import collections
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import engine  # noqa: E402

DEBUG_DLL = ROOT / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll"
LIB = ROOT / "lib"


def _spawn(env_extra=None, cmd=None, cwd=None):
    env = dict(os.environ, **(env_extra or {}))
    p = subprocess.Popen(cmd or engine.engine_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, cwd=cwd)

    def send(c):
        p.stdin.write(json.dumps(c) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())
    ready = json.loads(p.stdout.readline())
    return p, send, ready


def _nested_nulls(obj, depth=0, path=""):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if v is None and depth > 0:
                found.append(f"{path}.{k}")
            found += _nested_nulls(v, depth + 1, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _nested_nulls(v, depth, f"{path}[{i}]")
    return found


def test_ready_reports_localization_and_nested_nulls_are_omitted():
    p, send, ready = _spawn()
    try:
        assert ready["type"] == "ready" and ready["localization"] is True
        r = send({"cmd": "start_run", "character": "Regent", "seed": "nulls"})
        assert r["type"] == "decision"
        # Nested objects (cards, relics, ...) omit null fields ...
        assert _nested_nulls(r) == []
        # ... while a response's own fields are always present, null or not.
        q = send({"cmd": "quit"})
        assert "save" in q and q["save"] is None
    finally:
        p.kill()


def test_relocated_binary_still_finds_localization(tmp_path):
    if not DEBUG_DLL.is_file():
        pytest.skip("Debug build missing")
    out = tmp_path / "a" / "b"
    shutil.copytree(DEBUG_DLL.parent, out)
    cmd = [engine.find_dotnet() or "dotnet", str(out / "Sts2Headless.dll")]
    p, send, ready = _spawn({"STS2_LIB": str(LIB)}, cmd=cmd, cwd=str(tmp_path))
    try:
        assert ready["localization"] is True, ready
        r = send({"cmd": "start_run", "character": "Defect", "seed": "loc"})
        assert not r["context"]["act_name"].endswith(".title"), r["context"]
        assert not r["event_name"].endswith(".title")
    finally:
        p.kill()


def test_fast_map_pruning_matches_the_game():
    """STS2_VERIFY_MAP_PRUNING runs the game's FindMatchingSegments next to ours and throws on a difference."""
    p, send, _ = _spawn({"STS2_VERIFY_MAP_PRUNING": "1", "STS2_DEBUG_COMMANDS": "1"})
    try:
        for i, ch in enumerate(["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"] * 4):
            r = send({"cmd": "start_run", "character": ch, "seed": f"prune{i}", "ascension": i % 11})
            assert r["type"] == "decision", r
        assert send({"cmd": "__debug_stats"})["map_pruning_verified"] > 20
    finally:
        p.kill()


def test_quit_is_answered_promptly_after_ready():
    """Warm-up runs after ready; a command sent meanwhile waits for it, then runs normally."""
    t = time.time()
    p, send, ready = _spawn()
    try:
        assert send({"cmd": "start_run", "character": "Ironclad", "seed": "warm"})["type"] == "decision"
        assert send({"cmd": "quit"})["type"] == "quit_result"
        p.wait(timeout=10)
        assert time.time() - t < 30
    finally:
        p.kill()


def test_warmup_failure_is_reported_by_the_first_start_run():
    """Lazy init used to fail the first start_run; warm-up must not hide that error."""
    p, send, ready = _spawn({"STS2_DEBUG_COMMANDS": "1", "STS2_DEBUG_FAIL_WARMUP": "1"})
    try:
        assert ready["type"] == "ready"
        assert send({"cmd": "get_map"})["type"] == "error"  # no run yet, as without the failure
        r = send({"cmd": "start_run", "character": "Ironclad", "seed": "wf"})
        assert r["type"] == "error" and "simulated warm-up failure" in r["message"], r
        assert send({"cmd": "quit"})["type"] == "quit_result"
    finally:
        p.kill()


def _guard_dump(lines):
    return collections.Counter((name, int(n)) for _, n, name in (l.split("\t") for l in lines if l.strip()))


def test_setup_time_ui_guard_equals_runtime_guard(tmp_path):
    """tools/UiGuardPatcher (setup.sh) must guard exactly what HeadlessUiGuard guards at runtime."""
    original = LIB / "sts2.dll.original"
    stubs = ROOT / "src/GodotStubs/bin/Debug/net9.0"
    dotnet = engine.find_dotnet()
    if not (original.is_file() and stubs.is_dir() and DEBUG_DLL.is_file() and dotnet):
        pytest.skip("needs lib/sts2.dll.original, the GodotStubs and the Debug build")
    lib = tmp_path / "lib"
    shutil.copytree(LIB, lib)
    shutil.copy(original, lib / "sts2.dll")

    # Runtime (Harmony) guard on the unguarded DLL.
    rt = tmp_path / "runtime.txt"
    env = dict(os.environ, STS2_LIB=str(lib), STS2_UI_GUARD_RUNTIME="1", STS2_UI_GUARD_DUMP=str(rt))
    subprocess.run([dotnet, str(DEBUG_DLL)], input='{"cmd":"quit"}\n', text=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)

    # Setup-time (Cecil) guard on a copy of the same DLL.
    st = tmp_path / "setup.txt"
    r = subprocess.run([dotnet, "run", "--project", str(ROOT / "tools/UiGuardPatcher"), "-c", "Release", "--",
                        str(lib / "sts2.dll"), "--search", str(stubs), "--dump", str(st)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    runtime, setup = _guard_dump(rt.read_text().splitlines()), _guard_dump(st.read_text().splitlines())
    assert len(runtime) > 100
    assert runtime == setup

    # The stamped DLL is recognised: no runtime rewrite.
    log = subprocess.run([dotnet, str(DEBUG_DLL)], input='{"cmd":"quit"}\n', text=True,
                         env=dict(os.environ, STS2_LIB=str(lib)), capture_output=True, timeout=120).stderr
    assert "UI guard: pre-applied" in log


def test_engine_selection_prefers_newest_fresh_build(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "sts2.dll"
    lib.parent.mkdir()
    lib.write_text("x")
    builds = {}
    for name in ("R2R", "Release", "Debug"):
        d = tmp_path / name
        d.mkdir()
        (d / "Sts2Headless.dll").write_text("x")
        builds[name] = d / "Sts2Headless.dll"
    (tmp_path / "R2R" / "sts2.dll").write_text("x")  # R2R bundles the game DLL
    monkeypatch.setattr(engine, "ENGINE_CANDIDATES", [str(builds[n]) for n in ("R2R", "Release", "Debug")])
    monkeypatch.setattr(engine, "LIB_STS2", str(lib))
    monkeypatch.delenv("STS2_ENGINE_DLL", raising=False)

    def touch(path, t):
        os.utime(path, (t, t))
    now = time.time()
    touch(lib, now - 100)
    touch(builds["Debug"], now - 50)
    touch(builds["Release"], now - 200)
    touch(builds["R2R"], now - 10)
    touch(tmp_path / "R2R" / "sts2.dll", now - 10)
    assert engine.find_engine_dll() == str(builds["R2R"])
    touch(builds["Debug"], now)  # a later `dotnet build` wins
    assert engine.find_engine_dll() == str(builds["Debug"])
    touch(builds["Debug"], now - 50)
    touch(lib, now - 5)  # game DLL replaced after the R2R build: its bundled copy is stale
    assert engine.find_engine_dll() == str(builds["Debug"])
    monkeypatch.setenv("STS2_ENGINE_DLL", "/x/y.dll")
    assert engine.find_engine_dll() == "/x/y.dll"
