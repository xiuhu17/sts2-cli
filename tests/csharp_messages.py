"""Every error message the engine can send, read from its C# sources: the arguments of
Error(...) / BadRequest(...) calls and the reasons StrictActions / ResolveEnemyTarget return,
with each ternary branch expanded and every non-literal part as a hole (\\x00)."""
import os
import re

HOLE = "\x00"


def _read_str(s, i, expand=False):
    """s[i:] starts a C# string literal ($"..", @"..", ".."): (text, end), a hole for each
    {expr}. expand=True: (set of texts, end), a hole's literal alternatives expanded
    ({(n == 1 ? "is" : "are")})."""
    interp = verbatim = False
    while s[i] in "$@":
        interp |= s[i] == "$"
        verbatim |= s[i] == "@"
        i += 1
    assert s[i] == '"', s[i:i + 20]
    i += 1
    alts = [""]
    add = lambda xs: [a + x for a in alts for x in xs][:64]
    while True:
        c = s[i]
        if c == "\\" and not verbatim:
            nxt = s[i + 1]
            alts = add([{"n": "\n", "t": "\t", '"': '"', "\\": "\\", "'": "'"}.get(nxt, nxt)])
            i += 2
            continue
        if c == '"':
            if verbatim and s[i + 1] == '"':
                alts = add(['"']); i += 2; continue
            return (set(alts) if expand else alts[0]), i + 1
        if interp and c == "{":
            if s[i + 1] == "{":
                alts = add(["{"]); i += 2; continue
            depth, j = 1, i + 1
            while depth:
                if s[j] == '"' or (s[j] in "$@" and s[j + 1] in '"$@'):
                    _, j = _read_str(s, j); continue
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            inner = s[i + 1:j - 1]
            vals = set()
            if expand and '"' in inner:
                if re.search(r":[\w#.]+$", inner):  # a format suffix ({x:0.0})
                    inner = inner.rsplit(":", 1)[0]
                try:
                    vals = set(_P(_tokens(inner)).expr())
                except Exception:
                    vals = set()
            alts = add(sorted(vals) if vals else [HOLE])
            i = j
            continue
        if interp and c == "}" and s[i + 1] == "}":
            alts = add(["}"]); i += 2; continue
        alts = add([c])
        i += 1


def _tokens(s):
    """C# expression -> tokens: ('lit', text) / ('(',) / (')',) / ('?',) / (':',) / ('+',) / (',',) / ('x',)."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
        elif c == '"' or (c in "$@" and i + 1 < len(s) and s[i + 1] in '"$@'):
            txts, i = _read_str(s, i, expand=True)
            out.append(("lit", txts))
        elif c == "'":  # char literal
            j = s.index("'", i + 2 if s[i + 1] == "\\" else i + 1)
            out.append(("x",)); i = j + 1
        elif s.startswith("??", i) or s.startswith("?.", i) or s.startswith("?[", i):
            out.append(("x",)); i += 2
        elif c in "()?:+,":
            out.append((c,)); i += 1
        else:
            j = i
            while j < len(s) and not s[j].isspace() and s[j] not in "()?:+,\"'" and not (s[j] in "$@" and s[j + 1:j + 2] == '"'):
                j += 1
            out.append(("x",)); i = max(j, i + 1)
    return out


class _P:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i][0] if self.i < len(self.t) else None

    def expr(self):
        a = self.concat()
        if self.peek() == "?":
            self.i += 1
            b = self.expr()
            if self.peek() == ":":
                self.i += 1
            c = self.expr()
            return b | c
        return a

    def concat(self):
        vals = {""}
        while self.peek() not in (None, "?", ":", ")", ","):
            if self.peek() == "+":
                self.i += 1
                continue
            u = self.unit()
            vals = {a + b for a in vals for b in u}
        return vals

    def unit(self):
        k = self.peek()
        if k == "lit":
            self.i += 1
            return set(self.t[self.i - 1][1])
        if k == "(":
            self.i += 1
            v = self.expr()
            while self.peek() == ",":  # a tuple / call argument list: keep the literals
                self.i += 1
                v = v | self.expr()
            if self.peek() == ")":
                self.i += 1
            return v
        # an identifier, possibly called: a hole (its call's arguments are not the message)
        self.i += 1
        while self.peek() == "(":
            depth = 0
            while True:
                k2 = self.peek()
                self.i += 1
                depth += {"(": 1, ")": -1}.get(k2, 0)
                if depth == 0 or k2 is None:
                    break
        while self.peek() == "x":
            self.i += 1
        return {HOLE}


def _span(s, i, end=")"):
    """The text from i to the matching close (")" for a call, ";" for a statement)."""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == '"' or (c in "$@" and s[j + 1:j + 2] in ('"', "$", "@")):
            _, j = _read_str(s, j)
            continue
        if c == "'":
            j = s.index("'", j + 2 if s[j + 1] == "\\" else j + 1) + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return s[i:j]
            depth -= 1
        elif c == ";" and depth == 0 and end == ";":
            return s[i:j]
        j += 1
    return s[i:j]


def _strip_comments(s):
    out, i = [], 0
    while i < len(s):
        if s.startswith("//", i):
            i = s.find("\n", i)
            i = len(s) if i < 0 else i
            continue
        if s[i] == '"' or (s[i] in "$@" and s[i + 1:i + 2] in ('"', "$", "@")):
            _, j = _read_str(s, i)
            out.append(s[i:j]); i = j
            continue
        if s[i] == "'":
            j = s.index("'", i + 2 if s[i + 1] == "\\" else i + 1) + 1
            out.append(s[i:j]); i = j
            continue
        out.append(s[i]); i += 1
    return "".join(out)


def _values(expr):
    try:
        return {v for v in _P(_tokens(expr)).expr() if v.strip(HOLE + " ")}
    except Exception:
        return set()


def engine_messages(src_dir):
    """[(file, line, message)] for every engine error message."""
    out = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".cs"):
            continue
        s = _strip_comments(open(os.path.join(src_dir, fn), encoding="utf-8").read())
        for m in re.finditer(r"(?<![\w.])(?:Error|BadRequest|IllegalAction)\(", s):
            body = _span(s, m.end())
            args = body
            if m.group(0).startswith("IllegalAction"):  # (decision, action, why): the why
                args = body.split(",", 2)[-1]
            else:  # Error(message[, code]): drop the code
                args = re.sub(r',\s*"[a-z_]+"\s*$', "", body)
            line = s[:m.start()].count("\n") + 1
            for v in _values(args):
                out.append((fn, line, v))
        regions = []
        if fn == "StrictActions.cs":
            regions.append((s.find("internal static string? CheckLegal"), len(s)))
        if fn == "CombatParity.cs":
            a = s.find("ResolveEnemyTarget(Dictionary")
            regions.append((a, s.find("\n    }\n", a)))
        if fn == "AgentProtocol.cs":  # the reason of an argument the verb doesn't take
            a = s.find("var why1 =")
            regions.append((a, s.find(";", a) + 1))
        for a, b in regions:
            seg = s[a:b]
            starts = r"var why1 =\s*" if fn == "AgentProtocol.cs" else r"(?:\breturn\b|=>)\s*"
            for m in re.finditer(starts, seg):
                stmt = _span(seg, m.end(), ";")
                if '"' not in stmt:
                    continue
                line = s[:a + m.start()].count("\n") + 1
                for v in _values(stmt):
                    if " " in v.strip():  # a message, not a key or verb name
                        out.append((fn, line, v))
    return out
