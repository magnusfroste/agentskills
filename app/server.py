#!/usr/bin/env python3
"""AgentSkills: agents share skills over MCP, people decide what gets shared.

A skill is a folder of text with a SKILL.md, the open format Hermes, Claude Code and others read. Agents
publish versions over MCP and install approved ones from /.well-known/skills. A version reaches other agents
only after a person has approved it in the browser, since a skill is instructions and code an agent will run.

Environment:
  SKILLS_PASSWORD       the password people sign in with (20+ characters)
  MCP_KEYS              name:key pairs, comma separated, one per agent installation (keys 32+ characters)
  AGENT_NAMES           optional display names: name=Shown name,name2=Other (default: the key's name)
  SITE_TITLE            heading of the start page (default "Skills"); SITE_SUBTITLE below it
  PUBLIC_URL            e.g. https://skills.example.com, for the install addresses agents get
  TRUSTED_PROXY_HEADER  e.g. CF-Connecting-IP or X-Forwarded-For, when behind a proxy that sets it (for the lockout)
  REVIEW                off (default): published means shared. auto: shared unless the scan flags something. on: a person approves every version
  PUBLIC_INDEX          on or off (default): on lets anyone install approved skills from /.well-known/skills
  DATA_DIR / PORT       default /data, 8000

Routes: / | /s/<name> | POST /mcp | /i/<token>/.well-known/skills/... | /login | /health
"""
import hashlib
import hmac
import html
import http.cookies
import json
import os
import platform
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from store import Conflict, OUTCOMES, Store

VERSION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")).read().strip()
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
DATA = os.environ.get("DATA_DIR", "/data")
PASSWORD = os.environ.get("SKILLS_PASSWORD", "")
TITLE = os.environ.get("SITE_TITLE", "Skills")
SUBTITLE = os.environ.get("SITE_SUBTITLE", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PROXY_HEADER = os.environ.get("TRUSTED_PROXY_HEADER", "")
_review = os.environ.get("REVIEW", "off").strip().lower()
REVIEW = "off" if _review in ("off", "0", "false", "no", "") else "auto" if _review == "auto" else "on"  # anything unclear means a person looks
PUBLIC_INDEX = os.environ.get("PUBLIC_INDEX", "off").strip().lower() in ("on", "1", "true", "yes")
SESSION_DAYS, LOCKOUT_FAILS, LOCKOUT_SECONDS = 30, 10, 900
COOKIE = "as_session"
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
WELL_KNOWN = "/.well-known/skills"

store = Store(DATA)


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}", flush=True)


# --- people: password, session cookie, lockout ----------------------------------------------

SESSION_KEY = hmac.new(PASSWORD.encode(), b"agentskills-session-v1", hashlib.sha256).digest()
_fails, _fails_lock = {}, threading.Lock()


def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{exp}.{hmac.new(SESSION_KEY, exp.encode(), hashlib.sha256).hexdigest()}"


def session_ok(value):
    exp, _, mac = (value or "").partition(".")
    good = hmac.new(SESSION_KEY, exp.encode(), hashlib.sha256).hexdigest()
    return bool(mac) and hmac.compare_digest(mac, good) and exp.isdigit() and int(exp) > time.time()


def password_ok(given):
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest())


def locked(ip):
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if time.time() - t < LOCKOUT_SECONDS]
        _fails[ip] = recent
        return len(recent) >= LOCKOUT_FAILS


def failed(ip):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


# --- agents: keys and install addresses -------------------------------------------------------

def load_keys():
    keys = {}
    for pair in os.environ.get("MCP_KEYS", "").split(","):
        name, _, key = pair.strip().partition(":")
        if name and len(key) >= 32:
            keys[key] = name
        elif name:
            log(f"key for {name!r} ignored: shorter than 32 characters")
    return keys


KEYS = load_keys()


def install_token(key):
    """Skill installers fetch plain URLs and send no credentials, so the address is the credential. It is derived
    from the agent's key, can only read approved skills, and is not the key: the key never ends up in a URL."""
    return hmac.new(key.encode(), b"agentskills-install-v1", hashlib.sha256).hexdigest()[:40]


TOKENS = {install_token(k): n for k, n in KEYS.items()}
TOKEN_OF = {n: t for t, n in TOKENS.items()}
NAMES = {k.strip(): " ".join(v.split())[:60] for k, _, v in
         (p.partition("=") for p in os.environ.get("AGENT_NAMES", "").split(",")) if k.strip() and v.strip()}


def shown(name):
    return NAMES.get(name, name) if name else ""


def who_text(by, requested_by=None):
    agent_name = shown(by)
    return f"{requested_by} via {agent_name}" if requested_by and requested_by != agent_name else agent_name


def agent(header):
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    found = None
    for key, name in KEYS.items():
        if token and hmac.compare_digest(token, key):
            found = name
    return found


# --- tools ------------------------------------------------------------------------------------

def base_url(who):
    return f"{PUBLIC_URL or 'http://<this server>'}/i/{TOKEN_OF[who]}{WELL_KNOWN}"


def visible(info, who):
    """Other agents see only what a person approved; an author also sees its own versions, whatever their state."""
    return info["status"] == "approved" or info["by"] == who


def summary(meta, info):
    fb = store.feedback(meta["name"])
    return {"name": meta["name"], "description": info["description"], "version": info["version"], "owner": shown(meta["owner"]),
            "feedback": {o: sum(1 for f in fb if f["outcome"] == o) for o in OUTCOMES}}


def need(name):
    meta = store.get(name)
    if not meta:
        raise ValueError(f"No skill named {name!r}. search_skills lists what there is.")
    return meta


def t_search(a, who):
    out = {"skills": [summary(m, i) for m, i in store.search(str(a.get("query") or ""))][:int(a.get("limit") or 20)]}
    mine = []
    for meta in store.all():
        for info in store.versions(meta["name"]):
            if info["by"] == who and info["status"] != "approved":
                mine.append({"name": meta["name"], "version": info["version"], "status": info["status"],
                             "review": info["reviews"][-1] if info["reviews"] else None})
    if mine:
        out["your_unapproved_versions"] = mine
    return out


def t_read(a, who):
    meta = need(a.get("name"))
    version = a.get("version") or meta.get("approved")
    if not version:
        version = max((v["version"] for v in store.versions(meta["name"]) if v["by"] == who), default=None)
    info = store.info(meta["name"], version) if version else None
    if not info or not visible(info, who):
        raise ValueError(f"'{meta['name']}' has no version {a.get('version') or 'approved yet'} you can read.")
    path = str(a.get("path") or "SKILL.md")
    files = store.files(meta["name"], info["version"])
    if path not in files:
        raise ValueError(f"No file {path!r} in this skill. It has: {', '.join(sorted(files))}")
    return {"name": meta["name"], "version": info["version"], "latest_version": meta["version"], "status": info["status"], "sha256": info["sha256"],
            "owner": shown(meta["owner"]), "retired": meta.get("retired", False), "files": sorted(files), "path": path,
            "content": files[path].decode("utf-8")}


def t_publish(a, who):
    distinct = a.get("distinct_from")
    meta, info, changed = store.publish(a.get("files"), who, note=a.get("note"), expected_version=a.get("expected_version"),
                                        requested_by=a.get("requested_by"), distinct_from=distinct if isinstance(distinct, list) else None,
                                        auto_approve={"off": True, "auto": "clean", "on": False}[REVIEW])
    if changed:
        log(f"{who}: published {meta['name']} v{info['version']} ({info['status']}, {len(info['flags'])} flag(s))")
    state = ("Identical to the latest version, so nothing was added." if not changed else
             "Shared." if info["status"] == "approved" else
             "Waiting for a person to review it. Other agents cannot see it until then; search_skills shows you the outcome.")
    return {"name": meta["name"], "version": info["version"], "status": info["status"], "changed": changed, "flags": info["flags"],
            "url": f"{PUBLIC_URL}/s/{meta['name']}", "next": state}


def t_versions(a, who):
    meta = need(a.get("name"))
    return {"name": meta["name"], "approved": meta.get("approved"), "versions": [
        {"version": v["version"], "at": v["at"], "by": shown(v["by"]), "note": v["note"], "status": v["status"], "sha256": v["sha256"]}
        for v in store.versions(meta["name"]) if visible(v, who)]}


def t_feedback(a, who):
    meta = need(a.get("name"))
    version = a.get("version") or meta.get("approved")
    info = store.info(meta["name"], version) if version else None
    if not info or not visible(info, who):
        raise ValueError("Give a version you can read (read_skill shows the version).")
    entry = store.add_feedback(meta["name"], info["version"], who, a.get("outcome"), a.get("note"))
    log(f"{who}: feedback on {meta['name']} v{info['version']}: {entry['outcome']}")
    return {"recorded": entry, "next": "If you know the fix, publish it as the next version (publish_skill with expected_version)."}


def t_install(a, who):
    url = base_url(who)
    out = {"index": f"{url}/index.json", "note": "This address is yours and works without a key, so treat it like a password. "
           "Do not paste it into reports, skills or notes.", "hermes": {
               "search": f"hermes skills search {url}/index.json --source well-known",
               "install": f"hermes skills install well-known:{url}/<name>",
               "update": "hermes skills update"},
           "if_blocked": "Hermes scans every install and may refuse a skill. Tell the person what it found; do not work around the scan."}
    if a.get("name"):
        need(a["name"])
        out["hermes"]["install"] = f"hermes skills install well-known:{url}/{a['name']}"
        out["url"] = f"{url}/{a['name']}/SKILL.md"
    return out


S = {"type": "string"}
I = {"type": "integer"}
TOOLS = [
    ("search_skills", t_search, "Find approved skills by what they do; leave query empty to list them all. Also shows your own versions "
     "that are waiting for or failed review. Search before you write a skill.",
     {"query": S, "limit": I}, []),
    ("read_skill", t_read, "Read a skill: SKILL.md by default, or another file with path. Gives the approved version unless you name one.",
     {"name": S, "version": I, "path": S}, ["name"]),
    ("publish_skill", t_publish, "Publish a skill or an improvement as a new version. files maps relative paths to text, with SKILL.md at the "
     "top and name and description in its frontmatter; the name decides which skill it is. Earlier versions stay. To improve an existing "
     "skill give expected_version, the latest version you read; a new skill that looks like another is refused until you have compared them.",
     {"files": {"type": "object", "additionalProperties": S}, "note": {**S, "description": "What changed and why, for the reviewer"},
      "expected_version": I, "distinct_from": {"type": "array", "items": S, "description": "Skills you read and found to be different"},
      "requested_by": {**S, "description": "Who asked for this, if several people share this agent"}}, ["files"]),
    ("skill_versions", t_versions, "A skill's versions you can see, with who published them and their state.", {"name": S}, ["name"]),
    ("skill_feedback", t_feedback, "Say how a skill went after you used it: worked, failed or suggestion. A failed one needs a note on "
     "what went wrong. This is how skills get better, and it is what a reviewer reads.",
     {"name": S, "version": I, "outcome": {**S, "enum": list(OUTCOMES)}, "note": S}, ["name", "outcome"]),
    ("install_info", t_install, "Your address for installing approved skills, and the commands. Skills work best installed: an agent "
     "matches its task against the skills on its own disk.", {"name": S}, []),
]
BY_NAME = {n: fn for n, fn, *_ in TOOLS}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r},
             "annotations": {"readOnlyHint": n not in ("publish_skill", "skill_feedback"), "destructiveHint": False}}
            for n, _, d, p, r in TOOLS]


def rpc(msg, who):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "agentskills", "version": VERSION},
                   "instructions": f"{TITLE}: skills shared between agents. Before writing a skill, search_skills: improving one beats adding "
                                   "a second. install_info tells you how to install one so that it is there the next time the task comes up. "
                                   "After using a shared skill, tell skill_feedback how it went. Never put keys, passwords, customer data or "
                                   "install addresses in a skill: other agents read it."
                                   + {"on": " A person reviews every version before other agents can get it.", "auto": " A version the automatic scan flags waits for a person.", "off": ""}[REVIEW]})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        fn = BY_NAME.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
        try:
            result = fn(params.get("arguments") or {}, who)
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                       "structuredContent": result, "isError": False})
        except (ValueError, KeyError, TypeError) as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


# --- pages ------------------------------------------------------------------------------------

ASSETS = {n: open(os.path.join(UI, n), "rb").read() for n in os.listdir(UI)}
TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
STATUS_TEXT = {"pending": "Waiting for review", "approved": "Approved", "rejected": "Rejected", "revoked": "Revoked"}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def pill(status):
    return f'<span class="pill {esc(status)}">{esc(STATUS_TEXT.get(status, status))}</span>'


def when(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def layout(title, body, signed=True):
    out = ('<form method="post" action="/logout"><button class="btn quiet" type="submit">Sign out</button></form>' if signed else "")
    themes = "".join(f'<button type="button" data-theme-set="{t}" title="{t.capitalize()}">{t[0].upper()}</button>' for t in ("light", "dark", "system"))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><script src="/static/theme.js?v={VERSION}"></script>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{esc(title)}</title><link rel="icon" href="/static/favicon.svg"><link rel="stylesheet" href="/static/app.css?v={VERSION}">'
            f'<script src="/static/app.js?v={VERSION}" defer></script></head><body>'
            + (f'<header class="top"><a class="home" href="/">{esc(TITLE)}</a><div class="tools"><div class="theme">{themes}</div>{out}</div></header>' if signed else "")
            + f'<main>{body}</main></body></html>').encode()


def login_page(error="", nxt="/"):
    wrong = f'<p class="error">{esc(error)}</p>' if error else ""
    return layout(f"Sign in · {TITLE}", f'<form class="login card" method="post" action="/login"><h1>{esc(TITLE)}</h1>'
                  f'<input type="hidden" name="next" value="{esc(nxt)}"><label for="pw">Password</label>'
                  f'<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>{wrong}'
                  '<button class="btn primary" type="submit">Sign in</button></form>', signed=False)


def message(title, text):
    return layout(title, f'<section class="card"><h1>{esc(title)}</h1><p>{esc(text)}</p><p><a href="/">Back to the skills</a></p></section>')


def start_page():
    cards, waiting = [], 0
    for meta in store.all():
        versions = store.versions(meta["name"])
        pending = sum(1 for v in versions if v["status"] == "pending")
        waiting += pending
        info = next((v for v in versions if v["version"] == meta.get("approved")), versions[0] if versions else None)
        if not info:
            continue
        state = ('<span class="pill retired">Retired</span>' if meta.get("retired") else "") + (
            f'<span class="pill pending">{pending} to review</span>' if pending else "") + (
            f'<span class="pill approved">v{meta["approved"]}</span>' if meta.get("approved") else "")
        cards.append((not pending, meta["name"],
                      f'<a class="card skill" href="/s/{esc(meta["name"])}" data-search="{esc((meta["name"] + " " + info["description"]).lower())}">'
                      f'<h2>{esc(meta["name"])}</h2><p>{esc(info["description"])}</p>'
                      f'<footer><span>{esc(shown(meta["owner"]))}</span><span class="pills">{state}</span></footer></a>'))
    grid = "".join(c for _, _, c in sorted(cards, key=lambda c: c[:2])) or '<p class="muted">No skills yet. An agent publishes the first one with publish_skill.</p>'
    lead = f'<p class="attention">{waiting} version{"s" if waiting != 1 else ""} waiting for your review.</p>' if waiting else ""
    sub = f'<p class="muted">{esc(SUBTITLE)}</p>' if SUBTITLE else ""
    return layout(TITLE, f'<h1>{esc(TITLE)}</h1>{sub}{lead}<input id="filter" type="search" placeholder="Search skills" aria-label="Search skills">'
                  f'<div class="grid">{grid}</div>')


def actions(meta, info):
    name, n = esc(meta["name"]), info["version"]
    buttons = []
    if info["status"] != "approved":
        buttons.append(f'<button class="btn primary" data-action="approve">Approve v{n}</button>')
    if info["status"] == "pending":
        buttons.append(f'<button class="btn" data-action="reject">Reject</button>')
    if info["status"] == "approved":
        buttons.append(f'<button class="btn danger" data-action="revoke" data-confirm="Revoke v{n}? Agents will get the approved version before it.">Revoke</button>')
    buttons.append(f'<button class="btn quiet" data-action="{"unretire" if meta.get("retired") else "retire"}">{"Bring back" if meta.get("retired") else "Retire skill"}</button>')
    return (f'<div class="actions" data-skill="{name}" data-version="{n}"><input class="note" placeholder="Note for the author (optional)" aria-label="Review note">'
            + "".join(buttons) + "</div>")


def skill_page(name, version=None):
    meta = store.get(name)
    versions = store.versions(name) if meta else []
    if version:
        info = next((v for v in versions if v["version"] == version), None)
    else:
        info = next((v for v in versions if v["status"] == "pending"), None) or next(
            (v for v in versions if v["version"] == meta.get("approved")), None) if meta else None
        info = info or (versions[0] if versions else None)
    if not info:
        return None
    files = store.files(name, info["version"])
    base, diffs = store.diff(name, info["version"])
    flags = "".join(f'<tr><td>{esc(f["what"])}</td><td><code>{esc(f["file"])}:{f["line"]}</code></td><td><code>{esc(f["text"])}</code></td></tr>' for f in info["flags"])
    flags = (f'<section class="card"><h2>Worth a look ({len(info["flags"])})</h2><p class="muted">Pointers from an automatic scan. It blocks nothing and misses things: read the changes.</p>'
             f'<div class="scroll"><table>{flags}</table></div></section>') if flags else ""

    def line(l):
        cls = "add" if l.startswith("+") else "del" if l.startswith("-") else "hunk" if l.startswith("@@") else ""
        return f'<span class="{cls}">{esc(l)}</span>'

    changes = "".join(f'<details open><summary><code>{esc(p)}</code></summary><pre>{"".join(line(l) for l in d)}</pre></details>' for p, d in diffs)
    against = f"against approved v{base}" if base else "everything is new: no earlier approved version"
    listing = "".join(f'<details><summary><code>{esc(p)}</code> <span class="muted">{len(b):,} bytes</span></summary><pre>{esc(b.decode("utf-8"))}</pre></details>'
                      for p, b in sorted(files.items(), key=lambda x: (x[0] != "SKILL.md", x[0])))
    rows = "".join(f'<tr{" class=here" if v["version"] == info["version"] else ""}><td><a href="/s/{esc(name)}/v/{v["version"]}">v{v["version"]}</a></td><td>{pill(v["status"])}</td>'
                   f'<td>{esc(who_text(v["by"], v["requested_by"]))}</td><td>{esc(when(v["at"]))}</td><td>{esc(v["note"])}</td></tr>' for v in versions)
    feedback = "".join(f'<tr><td><span class="pill {esc(f["outcome"])}">{esc(f["outcome"])}</span></td><td>v{f["version"]}</td><td>{esc(shown(f["by"]))}</td>'
                       f'<td>{esc(when(f["at"]))}</td><td>{esc(f["note"])}</td></tr>' for f in reversed(store.feedback(name)[-50:]))
    reviews = "".join(f'<li>{esc(r["action"])} by {esc(r["by"])}, {esc(when(r["at"]))}{": " + esc(r["note"]) if r["note"] else ""}</li>' for r in info["reviews"])
    retired = '<p class="attention">Retired: agents no longer get this skill.</p>' if meta.get("retired") else ""
    return layout(f"{name} · {TITLE}", f'<p><a href="/">← All skills</a></p><h1>{esc(name)} {pill(info["status"])}</h1>{retired}<p>{esc(info["description"])}</p>'
                  f'<p class="muted">Version {info["version"]} of {meta["version"]}, by {esc(who_text(info["by"], info["requested_by"]))}, {esc(when(info["at"]))}. Owner: {esc(shown(meta["owner"]))}. '
                  f'Checksum <code>{esc(info["sha256"][:16])}</code></p>'
                  + (f'<p class="note-from">{esc(info["note"])}</p>' if info["note"] else "") + actions(meta, info) + (f"<ul>{reviews}</ul>" if reviews else "") + flags
                  + (f'<section class="card"><h2>Changes</h2><p class="muted">{against}</p>{changes}</section>' if diffs else "")
                  + f'<section class="card"><h2>Files</h2>{listing}</section><section class="card"><h2>Versions</h2><div class="scroll"><table>{rows}</table></div></section>'
                  + (f'<section class="card"><h2>Feedback from agents</h2><div class="scroll"><table>{feedback}</table></div></section>' if feedback else ""))


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "agentskills", ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def ip(self):
        # X-Forwarded-For can be a list; the last entry is the one our own proxy added.
        value = (self.headers.get(PROXY_HEADER) if PROXY_HEADER else None) or ""
        return value.split(",")[-1].strip() or self.client_address[0]

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def json(self, code, obj, headers=None):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json", headers)

    def ours(self, body, code=200):
        self.send(code, body, headers={"Content-Security-Policy": PAGE_CSP, "Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    def redirect(self, where, headers=None):
        self.send(303, b"", headers={"Location": where, **(headers or {})})

    def signed_in(self):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        return COOKIE in c and session_ok(c[COOKIE].value)

    def cookie(self, value, max_age):
        secure = "; Secure" if PUBLIC_URL.startswith("https://") else ""
        return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def body(self, limit):
        """The request body, read once. Connections are reused (HTTP/1.1): a body left unread would be taken
        for the start of the next request."""
        if getattr(self, "_body", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20 * 1024 * 1024:
                self.close_connection = True
                raise ValueError("too large")
            self._body = self.rfile.read(n)
        if len(self._body) > limit:
            raise ValueError("too large")
        return self._body

    def not_found(self):
        self.ours(message("Not found", "There is nothing at this address."), 404)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            return self.json(200, {"ok": True, "version": VERSION})
        if path.startswith("/static/") and path[8:] in ASSETS:
            return self.send(200, ASSETS[path[8:]], TYPES.get(os.path.splitext(path)[1], "application/octet-stream"), {"Cache-Control": "public, max-age=604800"})
        if path == "/mcp":
            return self.send(405, b"", headers={"Allow": "POST"})
        if path == "/robots.txt":
            return self.send(200, b"User-agent: *\nDisallow: /\n", "text/plain")
        m = re.match(r"^/i/([0-9a-f]{40})(/\.well-known/skills/.*)$", path)
        if m and m.group(1) in TOKENS:
            return self.installer(m.group(2), TOKENS[m.group(1)])
        if PUBLIC_INDEX and path.startswith(WELL_KNOWN + "/"):
            return self.installer(path, "public")
        if path.startswith("/i/") or path.startswith("/.well-known/"):
            return self.send(404, b"not found", "text/plain")
        if path == "/login":
            if self.signed_in():
                return self.redirect("/")
            return self.ours(login_page(nxt=urllib.parse.parse_qs(query).get("next", ["/"])[0]))
        if not self.signed_in():
            return self.redirect("/login?next=" + urllib.parse.quote(self.path, safe=""))
        if path == "/":
            return self.ours(start_page())
        m = re.match(r"^/s/([a-z0-9-]+)(?:/v/(\d+))?$", path)
        if m and store.get(m.group(1)):
            body = skill_page(m.group(1), int(m.group(2)) if m.group(2) else None)
            if body:
                return self.ours(body)
        return self.not_found()

    def installer(self, path, who):
        """The index and files in the format installers read. Only approved versions of skills not retired."""
        rest = urllib.parse.unquote(path[len(WELL_KNOWN) + 1:])
        cache = {"Cache-Control": "no-cache"}
        skills = store.installable()
        if rest == "index.json":
            return self.json(200, {"skills": [{"name": m["name"], "description": i["description"], "files": sorted(i["files"], key=lambda p: (p != "SKILL.md", p)),
                                               "version": i["version"], "sha256": i["sha256"]} for m, i in skills]}, cache)
        name, _, rel = rest.partition("/")
        info = next((i for m, i in skills if m["name"] == name), None)
        if not info or rel not in info["files"]:  # the manifest is the allow-list: a path never reaches the disk unchecked
            return self.send(404, b"not found", "text/plain")
        if rel == "SKILL.md":
            log(f"{who}: fetched {name} v{info['version']}")
        return self.send(200, store.files(name, info["version"])[rel], "text/plain; charset=utf-8", {**cache, "Content-Security-Policy": "sandbox"})

    def do_POST(self):
        self._body = None
        try:
            self.body(20 * 1024 * 1024)  # always consume it, whatever the route does with it
        except ValueError:
            return self.json(413, {"error": "too large"})
        path = self.path.partition("?")[0]
        if path == "/mcp":
            return self.mcp()
        if path == "/login":
            return self.login()
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": self.cookie("", 0)})
        # Everything else is a person acting in the browser: signed in, from our own page.
        if not self.signed_in():
            return self.json(401, {"error": "sign in"})
        same_origin = self.headers.get("X-Requested-With") == "agentskills" and (
            not self.headers.get("Origin") or urllib.parse.urlparse(self.headers["Origin"]).netloc == self.headers.get("Host"))
        if not same_origin:
            return self.json(403, {"error": "forbidden"})
        m = re.match(r"^/api/skills/([a-z0-9-]+)/(approve|reject|revoke|retire|unretire)$", path)
        if not m:
            return self.json(404, {"error": "not found"})
        name, action = m.groups()
        try:
            body = json.loads(self.body(10_000) or b"{}")
            if action in ("retire", "unretire"):
                store.set_retired(name, action == "retire", "browser")
            else:
                store.review(name, int(body.get("version")), action, "browser", body.get("note") if isinstance(body.get("note"), str) else None)
        except (KeyError, TypeError):
            return self.json(404, {"error": "not found"})
        except ValueError as e:
            return self.json(400, {"error": str(e)})
        log(f"browser ({self.ip()}): {action} {name} {body.get('version', '')}")
        return self.json(200, {"ok": True})

    def login(self):
        ip = self.ip()
        try:
            form = urllib.parse.parse_qs(self.body(10_000).decode())
        except ValueError:
            return self.send(400)
        nxt = form.get("next", ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
            nxt = "/"
        if locked(ip):
            return self.ours(login_page("Too many attempts. Try again in a while.", nxt), 429)
        if password_ok(form.get("password", [""])[0]):
            log(f"signed in from {ip}")
            return self.redirect(nxt, {"Set-Cookie": self.cookie(make_session(), SESSION_DAYS * 86400)})
        failed(ip)
        log(f"wrong password from {ip}")
        time.sleep(0.5)
        return self.ours(login_page("Wrong password.", nxt), 401)

    def mcp(self):
        who = agent(self.headers.get("Authorization", ""))
        if not who:
            return self.json(401, {"error": "Missing or wrong key. Send Authorization: Bearer <key>."}, {"WWW-Authenticate": "Bearer"})
        try:
            msg = json.loads(self.body(20 * 1024 * 1024) or b"null")
        except ValueError:
            return self.json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
        batch = msg if isinstance(msg, list) else [msg]
        answers = [a for a in (rpc(m, who) for m in batch if isinstance(m, dict)) if a is not None]
        if not answers:
            return self.send(202, b"", "application/json")
        return self.json(200, answers if isinstance(msg, list) else answers[0])


def import_folder(args):
    """python server.py import <folder of skill folders> [--by name]: bring in skills you already trust, approved."""
    by = args[args.index("--by") + 1] if "--by" in args else "import"
    root = args[0]
    for name in sorted(os.listdir(root)):
        top = os.path.join(root, name)
        if not os.path.isfile(os.path.join(top, "SKILL.md")):
            continue
        files = {}
        for dirpath, dirs, found in os.walk(top):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in found:
                if not f.startswith("."):
                    full = os.path.join(dirpath, f)
                    with open(full, "rb") as fh:
                        files[os.path.relpath(full, top).replace(os.sep, "/")] = fh.read()
        try:
            existing = store.get(name)
            meta, info, changed = store.publish(files, by, note="imported", expected_version=existing["version"] if existing else None,
                                                distinct_from=[m["name"] for m in store.all()], auto_approve=True)
            print(f"{name}: {'v' + str(info['version']) if changed else 'unchanged'}, {len(info['flags'])} flag(s)")
        except ValueError as e:
            print(f"{name}: skipped: {e}")


def banner(title, tagline, rows):
    """Printed once at start: which build this is and how it is set up. No secrets."""
    inner = max(len(title), len(tagline)) + 4
    print("\n".join([f"╭{'─' * inner}╮", f"│  {title.ljust(inner - 2)}│", f"│  {tagline.ljust(inner - 2)}│",
                     f"╰{'─' * inner}╯"] + [f"  {k:<9} {v}" for k, v in rows]), flush=True)


def main():
    if len(PASSWORD) < 20:
        log("SKILLS_PASSWORD must be at least 20 characters. Refusing to start.")
        sys.exit(1)
    if not KEYS:
        log("MCP_KEYS has no name:key pair with a key of 32+ characters: agents cannot connect.")
    port = int(os.environ.get("PORT", "8000"))
    skills = store.all()
    banner(f"AgentSkills {VERSION}", "Skills agents share over MCP, reviewed by people", [
        ("site", f"{TITLE} on :{port}, {PUBLIC_URL or '(PUBLIC_URL not set)'}"),
        ("mcp", f"POST :{port}/mcp, {len(TOOLS)} tools"),
        ("agents", f"{len(KEYS)} key(s): {', '.join(sorted(KEYS.values())) or '-'}"),
        ("skills", f"{len(skills)} ({sum(1 for m in skills if m.get('approved') and not m.get('retired'))} installable) in {DATA}"),
        ("review", {"on": "a person approves every version", "auto": "automatic, a person sees what the scan flags", "off": "off, published means shared"}[REVIEW]),
        ("index", "public" if PUBLIC_INDEX else "per agent, by private address"),
        ("python", f"{platform.python_version()}, started {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"),
    ])
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["import"]:
        import_folder(sys.argv[2:])
    else:
        main()
