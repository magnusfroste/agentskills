"""Skills on disk. No database: a skill is a folder, and every publish adds a version.

DATA_DIR/skills/<name>/
  meta.json        owner, latest version, the approved version, retired
  v0001/           the skill's files as published, never changed
  v0001.json       who published it, when, the note, checksums, what the checks found, and its review
  feedback.jsonl   what agents reported after using it

Nothing is ever overwritten or deleted. A version starts as pending; other agents only ever see the
highest approved one. Only a person can approve, reject, revoke or retire (see server.py).
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone

import checks

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAX_FILE, MAX_TOTAL, MAX_FILES, MAX_DEPTH = 1024 * 1024, 5 * 1024 * 1024, 100, 6
OUTCOMES = ("worked", "failed", "suggestion")
WORD = re.compile(r"[a-z0-9åäö]{3,}")
STOP = {"the", "and", "for", "with", "use", "when", "this", "that", "from", "your", "skill", "agent", "agents"}


class Conflict(ValueError):
    """The skill changed after the caller last read it."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def frontmatter(text):
    """The top-level scalar fields of a SKILL.md's frontmatter, or None if it has none. Nested fields are skipped."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, l in enumerate(lines[1:], 1) if l.strip() == "---")
    except StopIteration:
        return None
    out, i, body = {}, 0, lines[1:end]
    while i < len(body):
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", body[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in (">", "|"):
            block = []
            while i < len(body) and (not body[i].strip() or body[i][0] in " \t"):
                block.append(body[i].strip())
                i += 1
            value = " ".join(b for b in block if b)
        elif len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def checksum(files):
    return hashlib.sha256(b"".join(p.encode() + b"\0" + hashlib.sha256(b).digest()
                                   for p, b in sorted(files.items()))).hexdigest()


def words(text):
    return {w for w in WORD.findall((text or "").lower()) if w not in STOP}


def clean_files(files):
    """Check what an agent sent. Returns ({path: bytes}, frontmatter). Text only: what cannot be read cannot be reviewed."""
    if not isinstance(files, dict) or not files:
        raise ValueError("files: an object of relative path to text content, with SKILL.md at the top")
    if len(files) > MAX_FILES:
        raise ValueError(f"{len(files)} files; the limit is {MAX_FILES}")
    out, total = {}, 0
    for path, content in files.items():
        parts = str(path).split("/")
        if len(parts) > MAX_DEPTH or not all(SEGMENT.match(p) for p in parts):
            raise ValueError(f"file {path!r}: a relative path of letters, digits and . _ - (no .. and no hidden files)")
        if not isinstance(content, (str, bytes)):
            raise ValueError(f"file {path}: content must be text")
        try:
            data = content.encode("utf-8") if isinstance(content, str) else content
            data.decode("utf-8")
        except UnicodeError:
            raise ValueError(f"file {path}: not valid UTF-8. Skills are text only, so that a person can review them") from None
        if b"\0" in data:
            raise ValueError(f"file {path}: binary content. Skills are text only, so that a person can review them")
        if len(data) > MAX_FILE:
            raise ValueError(f"file {path}: {len(data)} bytes; the limit is {MAX_FILE}")
        total += len(data)
        out["/".join(parts)] = data
    if total > MAX_TOTAL:
        raise ValueError(f"{total} bytes in all; the limit is {MAX_TOTAL}")
    if "SKILL.md" not in out:
        raise ValueError("SKILL.md is missing: it goes at the top, with name and description in its frontmatter")
    fm = frontmatter(out["SKILL.md"].decode("utf-8"))
    if fm is None:
        raise ValueError("SKILL.md must start with frontmatter between --- lines, holding name and description")
    name, description = fm.get("name", ""), " ".join(fm.get("description", "").split())
    if not (NAME.match(name) and len(name) <= 64):
        raise ValueError(f"name {name!r} in the frontmatter: lowercase letters and digits, single hyphens between, at most 64")
    if not description or len(description) > 1024:
        raise ValueError("description in the frontmatter: required, at most 1024 characters. It is how an agent "
                         "decides to use the skill, so say what it does and when")
    return out, {"name": name, "description": description}


class Store:
    def __init__(self, data_dir):
        self.root = os.path.join(data_dir, "skills")
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.RLock()

    # --- files ------------------------------------------------------------------------------

    def _dir(self, name):
        return os.path.join(self.root, name)

    @staticmethod
    def _write(path, data):
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _write_json(self, path, obj):
        self._write(path, json.dumps(obj, ensure_ascii=False, indent=1).encode())

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    # --- reading ----------------------------------------------------------------------------

    def get(self, name):
        if not (name and NAME.match(name) and len(name) <= 64):
            return None
        return self._read_json(os.path.join(self._dir(name), "meta.json"))

    def all(self):
        found = [self.get(n) for n in os.listdir(self.root)]
        return sorted((m for m in found if m), key=lambda m: m.get("updated", ""), reverse=True)

    def info(self, name, version):
        return self._read_json(os.path.join(self._dir(name), f"v{int(version):04d}.json"))

    def versions(self, name):
        meta = self.get(name)
        found = [self.info(name, n) for n in range(1, meta["version"] + 1)] if meta else []
        return sorted((v for v in found if v), key=lambda v: v["version"], reverse=True)

    def files(self, name, version):
        """{path: bytes} of a version."""
        info = self.info(name, version)
        if not info:
            return {}
        out = {}
        for path in info["files"]:
            with open(os.path.join(self._dir(name), f"v{int(version):04d}", *path.split("/")), "rb") as f:
                out[path] = f.read()
        return out

    def installable(self):
        """(meta, info) of every skill other agents get: approved and not retired."""
        out = []
        for meta in self.all():
            if meta.get("approved") and not meta.get("retired"):
                out.append((meta, self.info(meta["name"], meta["approved"])))
        return sorted(out, key=lambda x: x[0]["name"])

    def search(self, query):
        """Best first, over what is installable: the name counts most, then the description, then the text."""
        q = words(query)
        scored = []
        for meta, info in self.installable():
            if not q:
                scored.append((0, meta, info))
                continue
            text = b" ".join(self.files(meta["name"], info["version"]).values()).decode("utf-8")
            score = (5 * len(q & words(meta["name"].replace("-", " "))) + 3 * len(q & words(info["description"]))
                     + len(q & words(text)))
            if score:
                scored.append((score, meta, info))
        return [(m, i) for _, m, i in sorted(scored, key=lambda x: (-x[0], x[1]["name"]))]

    def similar(self, name, description):
        """Skills that already seem to do this: the same words in the name or most of the description."""
        mine, about = words(name.replace("-", " ")), words(description)
        hits = []
        for meta in self.all():
            info = self.info(meta["name"], meta.get("approved") or meta["version"])
            if meta["name"] == name or meta.get("retired") or not info:
                continue
            theirs, text = words(meta["name"].replace("-", " ")), words(info["description"])
            if (mine and len(mine & theirs) / len(mine | theirs) >= .5) or (about and len(about & text) / len(about | text) >= .5):
                hits.append(meta["name"])
        return sorted(hits)

    def diff(self, name, version):
        """(the version compared with, [(path, unified diff lines)]): against the highest approved version before
        this one, which is what a reviewer is deciding whether to replace."""
        versions = self.versions(name)
        base = next((v["version"] for v in versions if v["version"] < version and v["status"] == "approved"), None)
        old, new, out = self.files(name, base) if base else {}, self.files(name, version), []
        for path in sorted(set(old) | set(new)):
            if old.get(path) != new.get(path):
                a = old.get(path, b"").decode("utf-8").splitlines()
                b = new.get(path, b"").decode("utf-8").splitlines()
                out.append((path, list(difflib.unified_diff(a, b, lineterm="", n=3))[2:][:4000]))
        return base, out

    def feedback(self, name):
        try:
            with open(os.path.join(self._dir(name), "feedback.jsonl"), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    # --- writing ----------------------------------------------------------------------------

    def publish(self, files, by, note=None, expected_version=None, requested_by=None, distinct_from=None, auto_approve=False):
        """Add a version. Returns (meta, info, changed): publishing what the latest version already holds adds nothing."""
        clean, fm = clean_files(files)
        name, stamp = fm["name"], now()
        sha = checksum(clean)
        with self.lock:
            meta = self.get(name)
            if meta:
                if meta.get("retired"):
                    raise ValueError(f"'{name}' is retired. A person can bring it back in the browser, or publish under a new name.")
                if expected_version is None:
                    raise ValueError(f"'{name}' exists, at version {meta['version']} by {meta['owner']}. Read it (read_skill) and "
                                     f"publish again with expected_version={meta['version']}, or choose another name if yours is a different skill.")
                if int(expected_version) != meta["version"]:
                    raise Conflict(f"'{name}' is at version {meta['version']}, not {expected_version}: someone published in "
                                   "between. Read it again (read_skill) and publish on top of that.")
                latest = self.info(name, meta["version"])
                if latest and latest["sha256"] == sha:
                    return meta, latest, False
            else:
                overlap = [s for s in self.similar(name, fm["description"]) if s not in (distinct_from or [])]
                if overlap:
                    raise ValueError(f"This looks like {', '.join(overlap)}. Read them (read_skill): improve one by publishing it "
                                     "with expected_version, or, if yours really is a different skill, publish again with "
                                     f"distinct_from={json.dumps(overlap)}.")
                meta = {"name": name, "owner": by, "created": stamp, "version": 0, "approved": None, "retired": False}
            n = meta["version"] + 1
            info = {"version": n, "at": stamp, "by": by, "requested_by": " ".join(str(requested_by or "").split())[:80] or None,
                    "note": (note or "").strip()[:500] or None, "description": fm["description"], "sha256": sha,
                    "files": {p: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for p, b in sorted(clean.items())},
                    "flags": checks.scan(clean), "status": "approved" if auto_approve else "pending", "reviews": []}
            os.makedirs(self._dir(name), exist_ok=True)
            final, tmp = os.path.join(self._dir(name), f"v{n:04d}"), os.path.join(self._dir(name), f".v{n:04d}.part")
            shutil.rmtree(tmp, ignore_errors=True)  # what a crash left, never a version
            for path, data in clean.items():
                full = os.path.join(tmp, *path.split("/"))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                self._write(full, data)
            os.rename(tmp, final)
            self._write_json(final + ".json", info)
            meta.update(version=n, updated=stamp, updated_by=by)
            if auto_approve:
                meta["approved"] = n
            self._write_json(os.path.join(self._dir(name), "meta.json"), meta)
            return meta, info, True

    def review(self, name, version, action, by, note=None):
        """approve, reject or revoke a version. Revoking the approved one puts the approved version before it back."""
        status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[action]
        with self.lock:
            meta, info = self.get(name), self.info(name, version) if self.get(name) else None
            if not info:
                raise KeyError(f"{name} v{version}")
            allowed = {"approve": ("pending", "rejected", "revoked"), "reject": ("pending",), "revoke": ("approved",)}[action]
            if info["status"] not in allowed:
                raise ValueError(f"v{version} is {info['status']}; it cannot be {status}")
            info["status"] = status
            info["reviews"].append({"action": action, "by": by, "at": now(), "note": (note or "").strip()[:500] or None})
            self._write_json(os.path.join(self._dir(name), f"v{int(version):04d}.json"), info)
            approved = [v["version"] for v in self.versions(name) if v["status"] == "approved"]
            meta.update(approved=max(approved) if approved else None, updated=now(), updated_by=by)
            self._write_json(os.path.join(self._dir(name), "meta.json"), meta)
            return meta, info

    def set_retired(self, name, retired, by):
        with self.lock:
            meta = self.get(name)
            if not meta:
                raise KeyError(name)
            meta.update(retired=bool(retired), retired_at=now() if retired else None, retired_by=by if retired else None)
            self._write_json(os.path.join(self._dir(name), "meta.json"), meta)
            return meta

    def add_feedback(self, name, version, by, outcome, note):
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome: one of {', '.join(OUTCOMES)}")
        note = (note or "").strip()[:2000]
        if outcome != "worked" and not note:
            raise ValueError("note: say what went wrong or what to change, so the next version can fix it")
        entry = {"at": now(), "by": by, "version": int(version), "outcome": outcome, "note": note or None}
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        with self.lock, open(os.path.join(self._dir(name), "feedback.jsonl"), "ab") as f:
            f.write(line)
        return entry
