"""
MEET_RUDI Training Arena — the local GENERATOR: a small web server on THIS computer that lets the
arena console (Generator pane, super-admins only) have batch files written by Claude Code, on the
super-admin's own Claude subscription.

Nobody has to type a command. Once per computer, double-click Install-Generator.cmd (next to this
file): it registers the `meetrudi-generator:` link for the current Windows user (no admin rights).
From then on the console's "Start the generator" button opens that link; Windows starts this
program without a window (pythonw, `--launched <link>`) and the console pairs with it on its own,
using the one-time code the link carries. "Stop" in the console ends it; so do 4 idle hours.
Started by hand instead (python generator.py), it prints a pairing code and runs until Ctrl+C.

What it does and does not do:
  - Listens on 127.0.0.1 only. A console pairs with it by a code — printed here, or carried by the
    start link — and every other request needs the token that pairing returns. Only the arena
    console's origin (and localhost, for development) may call it; other Host headers are refused
    (DNS rebinding). A start link opened by any other web site starts it, but that site cannot use
    it: its requests are refused by origin.
  - Runs `claude -p` (Claude Code) with no tools, no MCP servers and no project settings, in an
    empty folder and without console windows. ANTHROPIC_API_KEY and the other API / cloud-provider
    variables are removed from Claude Code's environment, so it can only use the subscription it
    is signed in with — it never spends API credit. If Claude Code does not answer, the console
    says so.
  - Sends Claude only the skill description, the arena's format rules and the persona sheet —
    synthetic text. Never Rudi's prompts, transcripts, results or keys.
  - Keeps every finished file in its history folder, and on request writes it into the repo's
    evals/arena-batches/generated/ folder. Saving to S3 is the console's job (the arena API).
Requires Python 3.10+ and PyYAML (pip install pyyaml).
"""

import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import gen_core  # noqa: E402
except ImportError:                  # PyYAML missing: only --install still runs (and installs it)
    gen_core = None

VERSION = "1"
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
REPO_FOLDER = os.path.join(REPO, "evals", "arena-batches", "generated")
HOME = os.path.join(os.path.expanduser("~"), ".meetrudi", "arena-generator")
ORIGINS = ["https://meet-rudi.github.io"]
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Variables that would make Claude Code use an API key or a cloud provider instead of the
# subscription it is signed in with. CLAUDE_CODE_OAUTH_TOKEN is a subscription token: kept.
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BEDROCK_BASE_URL",
             "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_VERTEX_BASE_URL", "AWS_BEARER_TOKEN_BEDROCK")
NO_TOOLS = "Bash Edit Write Read Glob Grep WebFetch WebSearch NotebookEdit Task TodoWrite SlashCommand BashOutput KillShell"
MAX_BODY = 2 * 1024 * 1024
PORT = 47815                         # unusual on purpose: 8765 and friends are often taken by other local tools
MAX_PAIR_TRIES = 10
SCHEME = "meetrudi-generator"        # the start link: meetrudi-generator://start?pair=<one-time code>
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PENDING_TTL_S = 120                  # a start link's code is good for two minutes
IDLE_MIN_LAUNCHED = 240              # started by the console: ends itself after 4 idle hours
NO_WINDOW = 0x08000000 if os.name == "nt" else 0         # CREATE_NO_WINDOW: no console flashing up


def stamp(t=None):
    return datetime.datetime.fromtimestamp(t or time.time()).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- Claude Code
class ClaudeCode:
    """`claude -p` on the subscription the CLI is signed in with."""

    def __init__(self, exe=None, timeout=360):
        # On Windows npm installs a shell script called `claude` next to `claude.cmd`; only the
        # .exe (native installer) or the .cmd can be started.
        names = ("claude.exe", "claude.cmd", "claude") if os.name == "nt" else ("claude",)
        self.exe = exe or next((p for p in (shutil.which(n) for n in names) if p), None)
        self.timeout = timeout
        self.workdir = tempfile.mkdtemp(prefix="arena-generator-")
        self._proc, self._lock = None, threading.Lock()

    def env(self):
        return {k: v for k, v in os.environ.items() if k not in STRIP_ENV}

    def version(self):
        out = subprocess.run([self.exe, "--version"], capture_output=True, text=True, timeout=30, env=self.env(),
                             creationflags=NO_WINDOW)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "unknown"

    def ask(self, prompt, model="sonnet"):
        cmd = [self.exe, "-p", "--output-format", "json", "--model", model, "--strict-mcp-config",
               "--setting-sources", "project", "--disallowedTools", NO_TOOLS]
        with self._lock:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          cwd=self.workdir, env=self.env(), text=True, encoding="utf-8",
                                          creationflags=NO_WINDOW)
        try:
            out, err = self._proc.communicate(prompt, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise gen_core.GenError("Claude Code did not answer within %d s." % self.timeout)
        finally:
            code = self._proc.returncode if self._proc else None
        try:
            d = json.loads(out)
        except ValueError:
            raise gen_core.GenError("Claude Code failed: %s" % ((err or out or "no output").strip().splitlines() or ["?"])[-1][:300])
        if d.get("is_error") or d.get("subtype") != "success":
            raise gen_core.GenError("Claude Code: %s" % str(d.get("result") or d.get("subtype") or "error")[:300])
        usage = d.get("usage") or {}
        return d.get("result") or "", {"ms": d.get("duration_ms"), "exit": code, "model": next(iter(d.get("modelUsage") or {}), model),
                                       "tokens_out": usage.get("output_tokens"),
                                       "tokens_in": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                                       + (usage.get("cache_creation_input_tokens") or 0)}

    def kill(self):
        p = self._proc
        if p and p.poll() is None:
            if os.name == "nt":                                      # claude.cmd starts node: end the whole tree
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
            else:
                p.send_signal(signal.SIGTERM)


# --------------------------------------------------------------------------- jobs and history
class Jobs:
    """One generation at a time (it is one person's subscription); each runs in a thread."""

    def __init__(self, claude, history):
        self.claude, self.history = claude, history
        self.jobs, self.lock, self.current = {}, threading.Lock(), None

    def start(self, kind, fn):
        with self.lock:
            if self.current and self.jobs[self.current]["state"] == "running":
                raise gen_core.GenError("Another generation is still running; wait for it or cancel it.")
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "kind": kind, "state": "running", "steps": [], "result": None, "error": None,
                   "started": time.time(), "cancel": False}
            self.jobs[jid], self.current = job, jid

        def progress(text, state="run", t=""):
            steps = job["steps"]
            if steps and steps[-1]["text"] == text:
                steps[-1].update(state=state, t=t)
            else:
                if steps and steps[-1]["state"] == "run":
                    steps[-1]["state"] = "done"
                steps.append({"text": text, "state": state, "t": t})

        def run():
            try:
                job["result"] = fn(progress, lambda: job["cancel"])
                job["state"] = "cancelled" if job["cancel"] else "done"
            except gen_core.Cancelled:
                job["state"] = "cancelled"
            except gen_core.GenError as e:
                job["state"], job["error"] = "failed", str(e)
            except Exception as e:  # noqa: BLE001 - shown to the super-admin, logged in full here
                traceback.print_exc()
                job["state"], job["error"] = "failed", "Unexpected error: %s" % type(e).__name__
            for s in job["steps"]:
                if s["state"] == "run":
                    s["state"] = "done" if job["state"] == "done" else "todo"
            log("job %s %s %s" % (jid, kind, job["state"] + (": " + job["error"] if job["error"] else "")))

        threading.Thread(target=run, daemon=True).start()
        return self.view(jid)

    def view(self, jid):
        j = self.jobs.get(jid)
        if not j:
            return None
        return {k: j[k] for k in ("id", "kind", "state", "steps", "result", "error")} | {"seconds": int(time.time() - j["started"])}

    def cancel(self, jid):
        j = self.jobs.get(jid)
        if j and j["state"] == "running":
            j["cancel"] = True
            self.claude.kill()
        return self.view(jid)


class History:
    """Every finished file, kept on this computer (not in the repo) so the console can reopen it."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def _path(self, hid):
        if not re.match(r"^[0-9a-f]{12}$", hid or ""):
            raise gen_core.GenError("Unknown history entry.")
        return os.path.join(self.folder, hid + ".json")

    def add(self, spec, out):
        hid = uuid.uuid4().hex[:12]
        entry = {"id": hid, "when": stamp(), "file": out["file"], "cases": len(out["rows"]), "spec": spec, "rows": out["rows"],
                 "result": out, "saved": []}
        with open(self._path(hid), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        return hid

    def get(self, hid):
        with open(self._path(hid), encoding="utf-8") as f:
            return json.load(f)

    def saved(self, hid, where):
        e = self.get(hid)
        e["saved"].append({"where": str(where)[:200], "when": stamp()})
        with open(self._path(hid), "w", encoding="utf-8") as f:
            json.dump(e, f, ensure_ascii=False)
        return e

    def list(self, limit=30):
        out = []
        for name in sorted(os.listdir(self.folder), key=lambda n: -os.path.getmtime(os.path.join(self.folder, n))):
            if name.endswith(".json") and len(out) < limit:
                try:
                    e = self.get(name[:-5])
                except (OSError, ValueError, gen_core.GenError):
                    continue
                out.append({"id": e["id"], "when": e["when"], "file": e["file"], "cases": e["cases"],
                            "saved": e["saved"][-1]["where"] if e["saved"] else ""})
        return out


def safe_name(name):
    base = os.path.basename(str(name or ""))
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,60}__input\.yaml$", base):
        raise gen_core.GenError("A file name must look like name__input.yaml (lowercase letters, digits, hyphens).")
    return base


def save_to_repo(folder, name, text):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name(name))
    if os.path.exists(path):
        root, ext = path[:-len("__input.yaml")], "__input.yaml"
        path = "%s-%s%s" % (root, datetime.datetime.now().strftime("%Y%m%d-%H%M"), ext)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------- the start link
def launch_nonce(url):
    """The one-time pairing code a start link carries — only a plain token, nothing else is used."""
    try:
        u = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return None
    if u.scheme != SCHEME:
        return None
    code = (urllib.parse.parse_qs(u.query).get("pair") or [""])[0]
    return code if NONCE_RE.match(code) else None


class Pending:
    """Codes from start links that found a generator already running: the second process hands its
    code over through this file and exits; the running one accepts it once, for two minutes."""

    def __init__(self, home):
        self.path = os.path.join(home, "pending-pairs.json")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [p for p in json.load(f) if time.time() - p.get("at", 0) < PENDING_TTL_S]
        except (OSError, ValueError):
            return []

    def _write(self, items):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(items, f)

    def add(self, code):
        self._write(self._read() + [{"code": code, "at": time.time()}])

    def take(self, code):
        items = self._read()
        hit = [p for p in items if secrets.compare_digest(p["code"], code)]
        if hit:
            self._write([p for p in items if p not in hit])
        return bool(hit)


def windowless_python():
    """pythonw.exe next to the running python.exe, so the console's start link opens no window."""
    d, name = os.path.split(sys.executable)
    w = os.path.join(d, name.lower().replace("python.exe", "pythonw.exe"))
    return w if os.path.exists(w) else sys.executable


def link_command():
    return '"%s" "%s" --launched "%%1"' % (windowless_python(), os.path.abspath(__file__))


def install():
    """Register meetrudi-generator: for the current Windows user (HKCU, no admin rights)."""
    import winreg
    root = r"Software\Classes\%s" % SCHEME
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:MEET RUDI Training Arena Generator")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, link_command())
    return link_command()


def uninstall():
    import winreg
    root = r"Software\Classes\%s" % SCHEME
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root + sub)
        except OSError:
            pass


def installed_command():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s\shell\open\command" % SCHEME) as k:
            return winreg.QueryValueEx(k, "")[0]
    except (ImportError, OSError):
        return None


# --------------------------------------------------------------------------- HTTP
class Server(ThreadingHTTPServer):
    """Holds its port alone. Python's default (SO_REUSEADDR) lets a second program bind the same
    port on Windows — a second start would not notice the first, and another program could sit on
    the generator's port."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def log(msg):
    print("%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


class App:
    def __init__(self, claude, port, origins, repo_folder, home, launch_code=None):
        self.claude, self.port = claude, port
        self.origins = origins
        self.repo_folder = repo_folder
        self.history = History(home)
        self.pending = Pending(home)
        self.jobs = Jobs(claude, self.history)
        self.code = "".join(secrets.choice(CROCKFORD) for _ in range(6))
        self.launch_code, self.launched = launch_code, bool(launch_code)
        self.tokens, self.bad_pairs = set(), 0
        self.info = {}
        self.claude_ok, self.claude_error = None, None      # None: still being checked
        self.last_seen = time.time()
        self.stop = None                                     # set by main(): ends the server

    def check_claude(self):
        """Does Claude Code answer on the subscription? Kept for the console to show."""
        try:
            text, meta = self.claude.ask('Reply with exactly: {"ok": true}', "sonnet")
            if not gen_core.parse_json(text).get("ok"):
                raise gen_core.GenError("unexpected answer")
            self.claude_ok, self.claude_error = True, None
            self.info["model_check"] = meta.get("model")
        except gen_core.GenError as e:
            self.claude_ok = False
            self.claude_error = ("Claude Code did not answer on this computer (%s). Open Claude Code once and sign in "
                                 "with your Claude subscription, then press Check again." % str(e)[:160])
        return self.claude_ok

    def pair_ok(self, code):
        code = str(code or "").strip()
        if code.upper() == self.code:
            return True
        if self.launch_code and secrets.compare_digest(code, self.launch_code):
            self.launch_code = None                          # a start link's code pairs once
            return True
        return bool(NONCE_RE.match(code)) and self.pending.take(code)

    def origin_ok(self, origin):
        return origin in self.origins or bool(LOCAL_ORIGIN.match(origin or ""))

    def host_ok(self, host):
        return (host or "").split(":")[0] in ("127.0.0.1", "localhost")

    # ---- routes (method, path, body, authed) -> (status, obj)
    def route(self, method, path, body, token):
        if path == "/health" and method == "GET":
            return 200, {"ok": True, "service": "meetrudi-arena-generator", "version": VERSION, "paired": token in self.tokens,
                         "launched": self.launched, "claude_ok": self.claude_ok, "claude_error": self.claude_error,
                         "claude": self.info, "repo_folder": os.path.relpath(self.repo_folder, REPO)}
        if path == "/pair" and method == "POST":
            if self.bad_pairs >= MAX_PAIR_TRIES:
                return 429, {"error": "locked", "detail": "Too many wrong codes. Stop the generator and start it again."}
            if not self.pair_ok(body.get("code")):
                self.bad_pairs += 1
                return 401, {"error": "bad_code", "detail": "That is not the code the generator gave."}
            tok = secrets.token_urlsafe(24)
            self.tokens.add(tok)
            log("paired a console tab")
            return 200, {"token": tok}
        if token not in self.tokens:
            return 401, {"error": "not_paired", "detail": "Pair the console with the generator first."}
        if path == "/shutdown" and method == "POST":
            log("stopped from the console")
            if self.stop:
                threading.Timer(0.3, self.stop).start()           # answer first, then stop
            return 200, {"ok": True}
        if path == "/recheck" and method == "POST":
            self.check_claude()
            return 200, {"claude_ok": self.claude_ok, "claude_error": self.claude_error}
        if path in ("/plan", "/redo", "/write") and not self.claude_ok:
            return 409, {"error": "claude_unavailable", "detail": self.claude_error or
                         "The generator is still checking that Claude Code answers; try again in a few seconds."}
        if path == "/personas" and method == "GET":
            return 200, {"personas": gen_core.library()}
        if path == "/plan" and method == "POST":
            spec = gen_core.normalize_spec(body.get("spec"))
            return 202, {"job": self.jobs.start("plan", lambda p, c: gen_core.plan(spec, self.claude.ask, p, c))}
        if path == "/redo" and method == "POST":
            spec = gen_core.normalize_spec(body.get("spec"))
            rows, idx = body.get("rows") or [], int(body.get("index") or 0)
            if not 0 <= idx < len(rows):
                return 400, {"error": "bad_index", "detail": "No such line in the plan."}
            return 202, {"job": self.jobs.start("redo", lambda p, c: gen_core.redo(spec, rows, body.get("personas"), idx, self.claude.ask))}
        if path == "/write" and method == "POST":
            spec = gen_core.normalize_spec(body.get("spec"))
            rows = body.get("rows") or []

            def work(p, c):
                out = gen_core.write(spec, rows, body.get("personas"), self.claude.ask, p, c)
                out["history_id"] = self.history.add(spec, out)
                return out
            return 202, {"job": self.jobs.start("write", work)}
        m = re.match(r"^/jobs/([0-9a-f]{12})(/cancel)?$", path)
        if m:
            view = self.jobs.cancel(m.group(1)) if m.group(2) and method == "POST" else self.jobs.view(m.group(1))
            return (200, {"job": view}) if view else (404, {"error": "not_found", "detail": "No such job."})
        if path == "/files" and method == "POST":
            p = save_to_repo(self.repo_folder, body.get("name"), str(body.get("text") or ""))
            if body.get("history_id"):
                self.history.saved(body["history_id"], "repo folder")
            log("saved %s" % os.path.relpath(p, REPO))
            return 200, {"path": p, "relative": os.path.relpath(p, REPO)}
        if path == "/history" and method == "GET":
            return 200, {"history": self.history.list()}
        m = re.match(r"^/history/([0-9a-f]{12})(/saved)?$", path)
        if m:
            if m.group(2) and method == "POST":
                return 200, {"entry": self.history.saved(m.group(1), body.get("where"))}
            return 200, {"entry": self.history.get(m.group(1))}
        return 404, {"error": "not_found", "detail": "No such route."}


def handler_for(app):
    class H(BaseHTTPRequestHandler):
        server_version = "meetrudi-arena-generator"

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and app.origin_ok(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Generator-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                if self.headers.get("Access-Control-Request-Private-Network") == "true":
                    self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if not app.host_ok(self.headers.get("Host")):
                self._send(421, {"error": "bad_host", "detail": "Use 127.0.0.1."})
                return False
            origin = self.headers.get("Origin")
            if origin and not app.origin_ok(origin):
                self._send(403, {"error": "bad_origin", "detail": "This page may not use the generator."})
                return False
            return True

        def do_OPTIONS(self):
            if not self._guard():
                return
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _handle(self, method):
            if not self._guard():
                return
            app.last_seen = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too_big", "detail": "Request too large."})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except ValueError:
                return self._send(400, {"error": "bad_json", "detail": "The request is not JSON."})
            try:
                status, obj = app.route(method, self.path.split("?")[0], body if isinstance(body, dict) else {},
                                        self.headers.get("X-Generator-Token"))
            except gen_core.SpecError as e:
                status, obj = 400, {"error": "bad_request", "detail": str(e)}
            except gen_core.GenError as e:
                status, obj = 409, {"error": "conflict", "detail": str(e)}
            except (OSError, ValueError) as e:
                status, obj = 400, {"error": "bad_request", "detail": str(e)[:200]}
            self._send(status, obj)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *a):
            pass
    return H


def _to_log(home):
    """Started without a window (pythonw) there is no console: everything goes to a log file."""
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "generator.log")
    if os.path.exists(path) and os.path.getsize(path) > 1000000:
        os.replace(path, path + ".1")
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)


def _running_here(port):
    """Is the generator itself (not some other program) answering on this port?"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("service") == "meetrudi-arena-generator"
    except (OSError, ValueError):
        return False


def _ensure_yaml():
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        print("  Installing PyYAML for this Python (one time)...", flush=True)
        return subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "pyyaml"]).returncode == 0


def setup(do_install):
    """What Install-Generator.cmd and Uninstall-Generator.cmd run (double-clicked, no typing)."""
    if os.name != "nt":
        print("The start link is for Windows. Elsewhere, start the generator with: python generator.py")
        return 2
    if not do_install:
        uninstall()
        print("Removed the Generator's start link for this Windows user. The console's Start button no longer works here.")
        return 0
    print("Training Arena Generator - one-time setup for this Windows user\n")
    yaml_ok = _ensure_yaml()
    claude = ClaudeCode()
    print("  Python       %s" % sys.executable)
    print("  PyYAML       %s" % ("ok" if yaml_ok else "MISSING - install it (pip install pyyaml) and run this again"))
    print("  Claude Code  %s" % (claude.exe or "NOT FOUND - install Claude Code and sign in with your Claude subscription"))
    shutil.rmtree(claude.workdir, ignore_errors=True)
    print("  Start link   %s: -> %s" % (SCHEME, install()))
    print("\nDone. In the arena console open Generator and press 'Start the generator'.")
    print("The first time, your browser asks whether the arena site may open it: tick 'Always allow' and open.")
    return 0 if yaml_ok and claude.exe else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Training Arena's local generator (Claude Code, your subscription).")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--origin", action="append", default=[], help="another console origin allowed to use the generator")
    ap.add_argument("--repo-folder", default=REPO_FOLDER, help="where 'Save into the repo folder' writes")
    ap.add_argument("--home", default=HOME, help="where the generator keeps its history and log")
    ap.add_argument("--claude", default=None, help="path to the claude CLI (default: from PATH)")
    ap.add_argument("--skip-check", action="store_true", help="do not check the Claude Code sign-in at start")
    ap.add_argument("--install", action="store_true", help="register the console's start link for this Windows user")
    ap.add_argument("--uninstall", action="store_true", help="remove the start link")
    ap.add_argument("--launched", metavar="LINK", default=None, help="(used by the start link) start without a window")
    ap.add_argument("--idle-minutes", type=int, default=None, help="stop after this long without a request (0: never)")
    a = ap.parse_args(argv)
    launched = a.launched is not None
    if launched:
        _to_log(a.home)
    for stream in (sys.stdout, sys.stderr):                  # a Windows console cannot print every character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if a.install or a.uninstall:
        return setup(a.install)
    if gen_core is None:
        print("PyYAML is missing. Double-click Install-Generator.cmd (next to generator.py) once.")
        return 2

    code = launch_nonce(a.launched) if launched else None
    if launched:
        log("started from the console%s" % ("" if code else " (the start link carried no usable code)"))
    claude = ClaudeCode(a.claude)
    if not claude.exe:
        print("Claude Code is not installed or not on PATH. Install it and sign in with your Claude subscription.")
        return 2
    stripped = [k for k in STRIP_ENV if k in os.environ]
    app = App(claude, a.port, ORIGINS + a.origin, a.repo_folder, a.home, launch_code=code)
    app.info = {"version": claude.version(), "api_key_removed": stripped, "subscription": True}
    if not launched and not a.skip_check:
        print("Checking that Claude Code answers on your subscription...", flush=True)
        if not app.check_claude():
            print(app.claude_error)
            return 2
    try:
        server = Server(("127.0.0.1", a.port), handler_for(app))
    except OSError:
        if launched and code and _running_here(a.port):
            Pending(a.home).add(code)                            # hand the code to the generator already running
            log("already running - handed the console's code over")
            return 0
        print("Port %d is in use by another program. Start with --port <another number>, and enter "
              "http://127.0.0.1:<that number> as the address in the console's connection window." % a.port)
        return 2
    app.stop = server.shutdown
    if launched and not a.skip_check:
        threading.Thread(target=app.check_claude, daemon=True).start()     # the console shows how it went
    elif a.skip_check:
        app.claude_ok = True
    idle = a.idle_minutes if a.idle_minutes is not None else (IDLE_MIN_LAUNCHED if launched else 0)
    if idle:
        def watchdog():
            while True:
                time.sleep(60)
                busy = app.jobs.current and app.jobs.jobs[app.jobs.current]["state"] == "running"
                if not busy and time.time() - app.last_seen > idle * 60:
                    log("no request for %d minutes - stopping" % idle)
                    server.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    if launched:
        log("listening on http://127.0.0.1:%d, Claude Code %s" % (a.port, app.info["version"]))
    else:
        print("\n  Training Arena generator - Claude Code %s, your subscription%s" % (
            app.info["version"], " (removed from its environment: %s)" % ", ".join(stripped) if stripped else ""))
        print("  Listening on http://127.0.0.1:%d - origins: %s + localhost" % (a.port, ", ".join(app.origins)))
        print("  Repo folder: %s" % os.path.relpath(a.repo_folder, REPO))
        print("\n  Pairing code:  %s\n" % app.code)
        print("  In the arena console: Generator > the connection pill > enter the code. Ctrl+C stops.\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
        shutil.rmtree(claude.workdir, ignore_errors=True)
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
