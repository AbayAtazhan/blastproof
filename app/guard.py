"""Static pre-flight review of agent-written code.

This is *not* the security boundary (the sandbox is). It is an early-warning
layer: it explains to the user, before execution, what the code will try to
do that a data analysis normally never needs. Findings are recorded in the
audit log so a reviewer can see intent, not only outcome.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

NETWORK_MODULES = {"socket", "requests", "urllib", "http", "httpx", "aiohttp", "ftplib",
                   "smtplib", "telnetlib", "paramiko", "websocket", "ssl"}
PROCESS_MODULES = {"subprocess", "multiprocessing", "pty", "ctypes", "cffi"}
DANGEROUS_CALLS = {
    "os.system": "runs a shell command",
    "os.popen": "runs a shell command",
    "os.fork": "forks a new process",
    "os.execv": "replaces the process", "os.execve": "replaces the process",
    "os.setuid": "tries to change user id", "os.setgid": "tries to change group id",
    "os.chmod": "changes file permissions",
    "os.remove": "deletes a file", "os.unlink": "deletes a file", "shutil.rmtree": "deletes a directory",
    "eval": "evaluates dynamic code", "exec": "executes dynamic code", "__import__": "dynamic import",
}
SAFE_WRITE_PREFIXES = ("/out", "/tmp")


@dataclass
class Finding:
    severity: str  # "high" | "medium" | "info"
    rule: str
    message: str
    line: int | None

    def to_dict(self) -> dict:
        return {"severity": self.severity, "rule": self.rule, "message": self.message, "line": self.line}


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def review(code: str) -> list[Finding]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [Finding("info", "syntax", f"code does not parse: {exc.msg}", exc.lineno)]

    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(sev: str, rule: str, msg: str, line: int | None):
        if (rule, msg) not in seen:
            seen.add((rule, msg))
            findings.append(Finding(sev, rule, msg, line))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root in NETWORK_MODULES:
                    add("high", "network", f"imports `{name}` (network access)", node.lineno)
                elif root in PROCESS_MODULES:
                    add("high", "process", f"imports `{name}` (process / native code control)", node.lineno)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in DANGEROUS_CALLS:
                sev = "medium" if name in {"eval", "exec", "__import__", "os.remove", "os.unlink"} else "high"
                add(sev, "call", f"calls `{name}` ({DANGEROUS_CALLS[name]})", node.lineno)
            if name == "open" and node.args:
                target = node.args[0]
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                mode_s = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else "r"
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    path = target.value
                    if any(c in mode_s for c in "wax+") and not path.startswith(SAFE_WRITE_PREFIXES):
                        add("high", "write", f"writes outside /out and /tmp: `{path}`", node.lineno)
                    elif path.startswith(("/etc", "/proc", "/sys", "/var/run", "/root", "/home")):
                        add("medium", "read", f"reads system path `{path}`", node.lineno)
        elif isinstance(node, ast.Attribute) and _dotted(node) in {"os.environ", "os.getenv"}:
            add("medium", "env", "reads environment variables (secret hunting?)", node.lineno)
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(n, ast.Break) for n in ast.walk(node)):
                add("medium", "loop", "unbounded `while True` loop with no break", node.lineno)

    return findings


def risk_level(findings: list[Finding]) -> str:
    sev = {f.severity for f in findings}
    return "high" if "high" in sev else "medium" if "medium" in sev else "low"
