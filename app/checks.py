"""What a reviewer should look at in a skill. It points things out and blocks nothing: a skill that talks about
rm -rf can be fine, and a pattern list will never catch everything. The reviewer decides."""
import re

PATTERNS = [
    ("pipes a download into a shell", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")),
    ("decodes and runs hidden content", re.compile(r"base64\s+(-d|--decode)|b64decode|\beval\s*\(|\bexec\s*\(")),
    ("touches credentials", re.compile(r"\.env\b|\.ssh/|id_rsa|/etc/(passwd|shadow)|\.hermes/|auth\.json|\.netrc", re.I)),
    ("tries to steer the agent", re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+"
                                            r"(instructions|prompts|rules)|do\s+not\s+(tell|mention|inform|show)\s+"
                                            r"(the\s+)?user|system\s+prompt", re.I)),
    ("destructive command", re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\bdrop\s+(table|database)\b|\bchmod\s+777\b|\bmkfs\b", re.I)),
]
HOST = re.compile(r"\bhttps?://([A-Za-z0-9.-]+)")
# Text a person does not see and a model reads: zero-width, bidi overrides and the tag block.
INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")


def scan(files):
    """files: {path: bytes}. Returns [{file, line, what, text}], at most 100."""
    found, hosts = [], {}
    for path, data in sorted(files.items()):
        for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
            if number == 1:
                line = line.lstrip("﻿")
            for what, pattern in PATTERNS:
                if pattern.search(line):
                    found.append({"file": path, "line": number, "what": what, "text": line.strip()[:200]})
            if INVISIBLE.search(line):
                found.append({"file": path, "line": number, "what": "invisible characters",
                              "text": INVISIBLE.sub("\N{REPLACEMENT CHARACTER}", line).strip()[:200]})
            for host in HOST.findall(line):
                hosts.setdefault(host.lower(), (path, number))
    for host, (path, number) in sorted(hosts.items()):
        found.append({"file": path, "line": number, "what": "talks to a host", "text": host})
    return found[:100]
