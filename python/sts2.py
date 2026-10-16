#!/usr/bin/env python3
"""sts2: a shell client for playing the headless engine one command per step.

Each call is a tiny round-trip to a background daemon (python/sts2_daemon.py) that holds
one engine process: started on first use, restarted (and the run replayed) if the engine
dies, stopped after an idle timeout. An agent that runs one Bash command per step pays
a few milliseconds of overhead per step instead of an engine start-up.

    sts2 start --character Ironclad --seed abc [--ascension N] [--lang zh] [--profile PATH]
    sts2 state                 current decision as text, with numbered legal actions
    sts2 do 3                  take legal action #3
    sts2 do "play Bash E0; play Strike; end"   several steps; stops at the first error or
                               when a step no longer fits the current decision
    sts2 deck | relics | potions | piles [draw] | map | card N | log [N]
    sts2 help                  all commands and action verbs
    sts2 --json <command>      raw engine JSON instead of text
    sts2 stop                  stop the daemon (and its engine)

Environment: STS2_SESSION (default "default") selects an independent daemon and game;
STS2_IDLE_TIMEOUT (seconds, default 1800) stops an unused daemon.

This file only imports C built-ins (no json/os/socket) so a call starts in ~20 ms;
everything else lives in the daemon.
"""

import sys

import _socket
import posix


def _env(name, default):
    v = posix.environ.get(name.encode())
    return v.decode() if v else default


def _paths(session):
    d = _env("TMPDIR", "/tmp")
    if len(d) > 70:  # AF_UNIX paths are limited to ~104 bytes on macOS
        d = "/tmp"
    base = d.rstrip("/") + f"/sts2-{posix.getuid()}-{session}"
    return base + ".sock", base + ".log"


def _spawn_daemon(session):
    import os
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    _, log = _paths(session)
    with open(log, "ab") as lf:
        subprocess.Popen([sys.executable, os.path.join(here, "sts2_daemon.py"), session],
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                         start_new_session=True, cwd=os.path.dirname(here))


def _connect(path):
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


def _request(session, payload, autostart=True):
    sock, log = _paths(session)
    try:
        s = _connect(sock)
    except OSError:
        if not autostart:
            return None
        _spawn_daemon(session)
        import time
        deadline = time.time() + 30
        while True:
            try:
                s = _connect(sock)
                break
            except OSError:
                if time.time() > deadline:
                    return 2, f"error: the sts2 daemon did not start; see {log}\n"
                time.sleep(0.02)
    try:
        s.sendall(payload)
        s.shutdown(_socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    finally:
        s.close()
    data = b"".join(chunks)
    if not data:
        return 2, "error: the sts2 daemon closed the connection (it may have crashed); run the command again\n"
    return int(data[:1]), data[1:].decode("utf-8", "replace")


def main(argv):
    session = _env("STS2_SESSION", "default")
    as_json = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--session" and i + 1 < len(argv):
            session = argv[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    if not rest or rest[0] in ("-h", "--help"):
        rest = ["help"]
    payload = (b"j" if as_json else b"-") + "\0".join(rest).encode()
    r = _request(session, payload, autostart=rest[0] != "stop")
    if r is None:
        sys.stdout.write("sts2 daemon is not running\n")
        return 0
    code, out = r
    sys.stdout.write(out)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
