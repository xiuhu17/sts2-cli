"""
Shared helpers for launching and talking to the headless engine (src/Sts2Headless).

- engine_command(): runs the already-built Sts2Headless.dll (see find_engine_dll) directly with `dotnet`,
  which skips MSBuild project evaluation (~0.25s per launch vs `dotnet run`).
  Falls back to `dotnet run --no-build --project ...` when the dll isn't built yet,
  or when STS2_USE_DOTNET_RUN=1.
- Engine: one engine process with a background stdout reader, per-response timeouts,
  and stderr drained (or discarded) so the pipe can never fill up and block the engine.
  A single Engine can play many runs: send start_run again to begin the next one.
"""

import json
import os
import queue
import shutil
import subprocess
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")
_BIN = os.path.join(ROOT, "src", "Sts2Headless", "bin")
# Debug is what `dotnet build` produces; R2R is the ReadyToRun (precompiled) build setup.sh
# publishes, which starts faster and steps ~30% faster. It must stay 5 levels below the repo.
ENGINE_DLL = os.path.join(_BIN, "Debug", "net9.0", "Sts2Headless.dll")
ENGINE_CANDIDATES = [
    os.path.join(_BIN, "R2R", "net9.0", "Sts2Headless.dll"),
    os.path.join(_BIN, "Release", "net9.0", "Sts2Headless.dll"),
    ENGINE_DLL,
]
LIB_STS2 = os.path.join(ROOT, "lib", "sts2.dll")


def find_engine_dll():
    """The engine to launch: $STS2_ENGINE_DLL, else the most recently built of R2R/Release/Debug.

    A build older than lib/sts2.dll is skipped when it carries its own copy of the game DLL
    (R2R), since it would run the previous game code. Newest-wins means a plain `dotnet build`
    after a source change is picked up even if an older R2R build exists.
    """
    env = os.environ.get("STS2_ENGINE_DLL")
    if env:
        return env
    lib_mtime = os.path.getmtime(LIB_STS2) if os.path.isfile(LIB_STS2) else 0
    best, best_mtime = None, -1.0
    for dll in ENGINE_CANDIDATES:
        if not os.path.isfile(dll):
            continue
        mtime = os.path.getmtime(dll)
        bundled = os.path.join(os.path.dirname(dll), "sts2.dll")
        if os.path.isfile(bundled) and os.path.getmtime(bundled) < lib_mtime:
            continue  # stale copy of the game DLL
        if mtime > best_mtime:
            best, best_mtime = dll, mtime
    return best


def find_dotnet():
    candidates = [
        os.environ.get("DOTNET"),
        os.path.expanduser("~/.dotnet-arm64/dotnet"),
        os.path.expanduser("~/.dotnet/dotnet"),
        shutil.which("dotnet"),
        "/opt/homebrew/opt/dotnet@9/bin/dotnet",
        "/usr/local/opt/dotnet@9/bin/dotnet",
    ]
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def engine_command(dotnet=None):
    dotnet = dotnet or find_dotnet() or "dotnet"
    dll = find_engine_dll()
    if dll and os.path.isfile(dll) and os.environ.get("STS2_USE_DOTNET_RUN") != "1":
        return [dotnet, dll]
    return [dotnet, "run", "--no-build", "--project", PROJECT]


class EngineError(RuntimeError):
    pass


class Engine:
    """A running headless engine process speaking the line-delimited JSON protocol."""

    def __init__(self, stderr_sink=None, timeout=60.0, env=None):
        # The engine answers engine_timeout itself after STS2_COMMAND_TIMEOUT (30s by
        # default); this client-side timeout is only a backstop for a dead process.
        """stderr_sink: None to discard engine logs, or a callable(line) to receive them."""
        self.timeout = timeout
        self.proc = subprocess.Popen(
            engine_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if stderr_sink else subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        if stderr_sink:
            threading.Thread(target=self._pump_stderr, args=(stderr_sink,), daemon=True).start()
        self.ready = self.read()
        if self.ready.get("type") != "ready":
            raise EngineError(f"Unexpected first message from engine: {self.ready}")

    def _pump_stdout(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self, sink):
        for line in self.proc.stderr:
            sink(line.rstrip("\n"))

    def read(self, timeout=None):
        """Next protocol message. Non-JSON stdout lines (stray library output) are skipped."""
        while True:
            try:
                line = self._lines.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise TimeoutError(f"No engine response within {timeout or self.timeout}s") from None
            if line is None:
                raise EngineError("Engine exited (EOF on stdout)")
            if line.startswith("{"):
                msg = json.loads(line)
                if msg.get("type") == "fatal":
                    raise EngineError(f"Engine crashed: {msg.get('message')}")
                if msg.get("code") in ("engine_timeout", "restart_required"):
                    # The engine process is wedged and refuses further commands.
                    raise EngineError(f"Engine needs a restart: {msg.get('message')}")
                return msg

    def send(self, cmd, timeout=None):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self.read(timeout)

    def action(self, action, **args):
        cmd = {"cmd": "action", "action": action}
        if args:
            cmd["args"] = args
        return self.send(cmd)

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
