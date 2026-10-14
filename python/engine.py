"""
Shared helpers for launching and talking to the headless engine (src/Sts2Headless).

- engine_command(): runs the already-built Sts2Headless.dll directly with `dotnet`,
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
ENGINE_DLL = os.path.join(ROOT, "src", "Sts2Headless", "bin", "Debug", "net9.0", "Sts2Headless.dll")


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
    if os.path.isfile(ENGINE_DLL) and os.environ.get("STS2_USE_DOTNET_RUN") != "1":
        return [dotnet, ENGINE_DLL]
    return [dotnet, "run", "--no-build", "--project", PROJECT]


class EngineError(RuntimeError):
    pass


class Engine:
    """A running headless engine process speaking the line-delimited JSON protocol."""

    def __init__(self, stderr_sink=None, timeout=30.0, env=None):
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
