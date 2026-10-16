#!/usr/bin/env python3
"""HTTP bridge for STS2 headless game. Lets Claude Code interact one command at a time.

Usage:
    python3 agent/sts2_bridge.py [port] [--compact] [--log FILE]
    python3 agent/sts2_bridge.py replay <logfile> [--until STEP] [--port PORT]

Examples:
    # Normal play with logging
    python3 agent/sts2_bridge.py 9876 --compact --log /tmp/game.jsonl

    # Replay a logged game up to step 42, then continue interactively
    python3 agent/sts2_bridge.py replay /tmp/game.jsonl --until 42 --port 9876
"""
import json, subprocess, os, sys, threading, re, time
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
from engine import engine_command  # noqa: E402

# --- Arg parsing ---
COMPACT = "--compact" in sys.argv
REPLAY_MODE = len(sys.argv) > 1 and sys.argv[1] == "replay"
LOG_FILE = None
REPLAY_FILE = None
REPLAY_UNTIL = None
PORT = 9876

if REPLAY_MODE:
    # replay <logfile> [--until STEP] [--port PORT]
    REPLAY_FILE = sys.argv[2] if len(sys.argv) > 2 else None
    for i, a in enumerate(sys.argv):
        if a == "--until" and i + 1 < len(sys.argv): REPLAY_UNTIL = int(sys.argv[i + 1])
        if a == "--port" and i + 1 < len(sys.argv): PORT = int(sys.argv[i + 1])
else:
    filtered = [a for a in sys.argv[1:] if not a.startswith("--")]
    PORT = int(filtered[0]) if filtered else 9876
    for i, a in enumerate(sys.argv):
        if a == "--log" and i + 1 < len(sys.argv): LOG_FILE = sys.argv[i + 1]

# --- JSON helpers ---
# --compact shrinks responses without hiding rules text: descriptions, enchantments and
# afflictions stay (STS2 content is new to most models, so the text is the information).
# Dropped: ids/text keys, nulls and empty lists, the full deck (get_deck has it), and
# upgrade previews except where the choice is an upgrade.
_STRIP_KEYS = {"id", "text_key", "act_name"}
_PILE_KEYS = {"draw_pile", "discard_pile", "exhaust_pile"}


def compact_pile(pile):
    """[{name:"Strike+", count:2, ...}] -> ["Strike+ x2"] (upgrades are already in the name)."""
    out = []
    for c in pile:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or "?"
        if c.get("enchantment"):
            name += f" [{c['enchantment']}]"
        if c.get("affliction"):
            name += f" [{c['affliction']}]"
        out.append(f"{name} x{c['count']}" if c.get("count", 1) > 1 else name)
    return out


def _fold_card(d):
    """Enchantment/affliction folded into the name: same information, fewer keys."""
    for key in ("enchantment", "affliction"):
        if d.get(key):
            amt = d.get(key + "_amount")
            d["name"] = f"{d.get('name')} [{d[key]}{' ' + str(amt) if amt else ''}]"
            d.pop(key, None)
            d.pop(key + "_amount", None)


def compact_json(obj, depth=0, keep_upgrade=False):
    if isinstance(obj, dict):
        if depth == 0:
            keep_upgrade = obj.get("decision") == "card_select" and obj.get("purpose") == "upgrade"
        result = {}
        for k, v in obj.items():
            if k in _STRIP_KEYS or v is None or v == [] or v == {}:
                continue
            if k == "after_upgrade" and not keep_upgrade:
                continue
            if k == "deck" and isinstance(v, list):
                continue  # the deck is in every decision; get_deck returns it on demand
            if k in _PILE_KEYS and isinstance(v, list):
                result[k] = compact_pile(v)
                continue
            if k == "vars" and obj.get("description"):
                continue  # already filled into the description
            result[k] = compact_json(v, depth + 1, keep_upgrade)
        if "enchantment" in result or "affliction" in result:
            _fold_card(result)
        return result
    if isinstance(obj, list):
        return [compact_json(v, depth + 1, keep_upgrade) for v in obj]
    return obj


_CTRL = re.compile(r'[\x00-\x1f\x7f]+')


def sanitize_json(obj):
    # Control characters become a space (descriptions use \n between sentences), and most
    # strings have none, so test before substituting.
    if isinstance(obj, str): return _CTRL.sub(' ', obj) if _CTRL.search(obj) else obj
    if isinstance(obj, dict): return {k: sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list): return [sanitize_json(v) for v in obj]
    return obj


# --- Game process ---
class Game:
    """One engine process. If it dies (crash, EOF, engine_timeout) it is started again and
    the run so far is replayed from memory (runs are deterministic from seed + actions)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.step = 0
        self.history = []       # state-changing requests of the current run
        self.last_resp = None   # last decision, for GET /state
        # The engine finds the game DLLs in ./lib (copied by setup.sh); STS2_GAME_DIR is
        # only an extra fallback, so don't override one the user already set.
        os.environ.setdefault("STS2_GAME_DIR", os.path.expanduser(
            "~/Library/Application Support/Steam/steamapps/common/"
            "Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"))
        self._spawn()

    def _spawn(self):
        self.proc = subprocess.Popen(
            engine_command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        proc = self.proc

        def _forward_stderr():
            for line in proc.stderr:
                print(f"[GAME] {line.rstrip()}", file=sys.stderr)
        threading.Thread(target=_forward_stderr, daemon=True).start()
        ready = self._read()
        print(f"Game ready: {ready}", file=sys.stderr)

    def _read(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"type": "error", "code": "engine_exited", "message": "EOF - game process ended"}
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)

    def _alive(self):
        return self.proc.poll() is None

    def _roundtrip(self, cmd):
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return {"type": "error", "code": "engine_exited", "message": "engine pipe closed"}
        return self._read()

    def _dead(self, resp):
        return resp.get("type") == "fatal" or resp.get("code") in ("engine_exited", "engine_timeout", "restart_required")

    def _respawn(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        self._spawn()
        last = None
        for i, cmd in enumerate(self.history):
            last = self._roundtrip(cmd)
            if self._dead(last):
                self.history = self.history[:i]
                break
        if last and last.get("type") == "decision":
            self.last_resp = last
        return len(self.history), last

    def send(self, cmd):
        with self.lock:
            if not self._alive():
                self._respawn()
            resp = self._roundtrip(cmd)
            if self._dead(resp):
                replayed, last = self._respawn()
                return self.step, {"type": "error", "code": "engine_restarted",
                                   "message": f"The engine failed ({resp.get('message')}); it was restarted and "
                                              f"{replayed} commands of this run were replayed. The command was not applied.",
                                   "replayed": replayed, "state": last}
            self.step += 1
            kind = cmd.get("cmd")
            if kind in ("start_run", "load_save"):
                self.history = [cmd] if resp.get("type") == "decision" else self.history
            elif kind == "action" and (resp.get("type") != "error" or resp.get("code") == "flow_failed"):
                self.history.append(cmd)
            elif kind == "quit":
                self.history = []
            if resp.get("type") == "decision":
                self.last_resp = resp
            return self.step, resp


# --- Logging ---
_log_fh = None

def log_entry(step, request, response):
    global _log_fh
    if _log_fh is None and LOG_FILE:
        _log_fh = open(LOG_FILE, "w")
    if _log_fh:
        entry = {"step": step, "ts": time.time(), "req": request, "resp": response}
        _log_fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _log_fh.flush()


# --- Replay mode ---
def do_replay():
    if not REPLAY_FILE:
        print("Usage: sts2_bridge.py replay <logfile> [--until STEP] [--port PORT]", file=sys.stderr)
        sys.exit(1)

    print(f"Replaying {REPLAY_FILE} until step {REPLAY_UNTIL or 'end'}...", file=sys.stderr)
    global game
    game = Game()

    with open(REPLAY_FILE) as f:
        entries = [json.loads(line) for line in f if line.strip()]

    replayed = 0
    last_resp = None
    for entry in entries:
        step = entry["step"]
        if REPLAY_UNTIL and step > REPLAY_UNTIL:
            break
        req = entry["req"]
        step_num, resp = game.send(req)
        resp = sanitize_json(resp)
        last_resp = resp
        replayed += 1

        dec = resp.get("decision", resp.get("type", "?"))
        hp = resp.get("hp", resp.get("player", {}).get("hp", "")) if isinstance(resp, dict) else ""
        print(f"  Step {step}: {req.get('cmd','?')}/{req.get('action','')} → {dec} (hp={hp})", file=sys.stderr)

    print(f"\nReplayed {replayed} steps. Game is at step {replayed}.", file=sys.stderr)
    if last_resp:
        dec = last_resp.get("decision", last_resp.get("type", "?"))
        print(f"Current state: {dec}", file=sys.stderr)
        # Print the full current state for debugging
        print(json.dumps(last_resp, ensure_ascii=False, indent=2)[:2000], file=sys.stderr)

    # Now start interactive bridge from this point
    print(f"\nStarting interactive bridge on port {PORT}...", file=sys.stderr)

    HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()


# --- Normal mode ---
game = None


HELP = {
    "type": "help",
    "post": "POST a protocol command as JSON, e.g. {\"cmd\":\"action\",\"action\":\"end_turn\"}",
    "get": {"/state": "last decision (no game change)", "/help": "this text"},
    "engine_help": "POST {\"cmd\":\"help\"} for the engine's command list",
    "shutdown": "POST {\"cmd\":\"shutdown\"} stops the bridge",
}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"null")
            except ValueError as e:
                return self._reply({"type": "error", "code": "bad_request", "message": f"body is not JSON: {e}"}, 400)
            if not isinstance(data, dict) or not isinstance(data.get("cmd"), str):
                return self._reply({"type": "error", "code": "bad_request",
                                    "message": "body must be a JSON object with a string 'cmd'"}, 400)
            if data["cmd"] == "shutdown":
                self._reply({"type": "shutdown"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            step, result = game.send(data)
            if LOG_FILE or not COMPACT:
                raw_result = sanitize_json(result)
                # Log FULL response (before compact) for replay
                log_entry(step, data, raw_result)
                self._reply(compact_json(raw_result) if COMPACT else raw_result)
            else:
                # Nothing to log: compact first so the sanitizer skips the fields compact drops.
                self._reply(sanitize_json(compact_json(result)))
            if data["cmd"] == "quit":
                # Keep serving: the next start_run gets a fresh engine.
                with game.lock:
                    game._respawn()
        except Exception as e:
            try:
                self._reply({"type": "error", "code": "bridge_exception", "message": repr(e)}, 500)
            except Exception:
                pass

    def do_GET(self):
        path = self.path.rstrip("/") or "/help"
        if path == "/state":
            st = game.last_resp or {"type": "error", "message": "No decision yet: POST start_run"}
            return self._reply(compact_json(st) if COMPACT else st)
        if path == "/help":
            return self._reply(HELP)
        return self._reply({"type": "error", "code": "not_found", "message": f"unknown path {path}; try /state or /help"}, 404)

    def log_message(self, fmt, *args):
        pass  # silent


if REPLAY_MODE:
    do_replay()
    sys.exit(0)

game = Game()
mode = "compact" if COMPACT else "full"
log_info = f", logging to {LOG_FILE}" if LOG_FILE else ""
print(f"STS2 bridge on port {PORT} (mode={mode}{log_info})", file=sys.stderr)
HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
