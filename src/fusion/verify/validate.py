"""Static validation of a check script before it ever runs (Harness²'s validation-before-rollout).

``validate_check`` returns the reasons a check may not run, and an empty list for one that may.
It rejects a check that

* is too big (``MAX_CHECK_BYTES``, ``MAX_CHECK_LINES``) or has a name that is not an identifier;
* does not compile: Python is compiled in memory (what ``py_compile`` does, without writing a
  ``.pyc``), shell is parsed with ``bash -n`` (parse only, nothing runs);
* reaches for the network: Python imports of network modules, dynamic imports (``__import__``,
  ``importlib``) that would hide one, network tools named as arguments; shell commands such as
  ``curl``, ``wget``, ``nc`` and bash's ``/dev/tcp``;
* writes outside its directory: a write (``open(..., "w")``, ``Path.write_text``, ``os.remove``,
  ``shutil.*``; shell redirects, ``cp``, ``mv``, ``rm``, ``tee``, ``cd`` ...) to an absolute path,
  a ``~`` path or one with a ``..`` segment.

These are heuristics that catch the obvious cases cheaply and say why. They are not the
boundary: a check that hides what it does still runs only in ``fusion.bench.sandbox``, which blocks
the network and every write outside the copy where the platform allows (see SECURITY.md).
"""

from __future__ import annotations

import ast
import re
import shlex
import shutil
import subprocess

from fusion.verify.synthesis import CheckScript

__all__ = ["MAX_CHECK_BYTES", "MAX_CHECK_LINES", "validate_check"]

MAX_CHECK_BYTES = 8_000
MAX_CHECK_LINES = 200
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_NETWORK_MODULES = frozenset(
    {
        "socket",
        "socketserver",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "xmlrpc",
        "websocket",
        "websockets",
        "paramiko",
        "multiprocessing.connection",
    }
)
_NETWORK_TOOLS = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "telnet",
        "socat",
    }
)
_DYNAMIC = frozenset({"__import__", "exec", "eval"})
_OPEN_WRITE = re.compile(r"[wax+]")
# Path / os / shutil operations that change the file system, by the name they are called with.
_PATH_WRITES = frozenset(
    {
        "write_text",
        "write_bytes",
        "touch",
        "mkdir",
        "unlink",
        "rmdir",
        "rename",
        "replace",
        "symlink_to",
        "hardlink_to",
        "chmod",
    }
)
_OS_WRITES = frozenset(
    {
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "mkdir",
        "makedirs",
        "chmod",
        "chown",
        "symlink",
        "link",
        "truncate",
    }
)
_SHUTIL_WRITES = frozenset(
    {"copy", "copy2", "copyfile", "copytree", "move", "rmtree", "copymode", "copystat"}
)
_SHELL_WRITES = frozenset(
    {
        "cp",
        "mv",
        "rm",
        "rmdir",
        "touch",
        "mkdir",
        "tee",
        "ln",
        "chmod",
        "chown",
        "install",
        "truncate",
    }
)
_SAFE_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n", "then", "do", "else", "!"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})


def validate_check(script: CheckScript) -> list[str]:
    """Why ``script`` may not run (empty when it may)."""
    size = len(script.body.encode("utf-8"))
    if size > MAX_CHECK_BYTES or script.body.count("\n") > MAX_CHECK_LINES:
        return [
            f"too big: {size} bytes and {script.body.count(chr(10))} lines "
            f"(at most {MAX_CHECK_BYTES} bytes and {MAX_CHECK_LINES} lines)"
        ]
    errors: list[str] = []
    if not _NAME.match(script.name):
        errors.append(f"name {script.name!r} must be letters, digits, '_' or '-' (1-48 chars)")
    if script.language == "python":
        errors += _python(script)
    else:
        errors += _shell(script.body)
    return errors


def outside(path: str) -> bool:
    """Does ``path`` point outside the directory the check runs in?"""
    text = path.strip()
    if not text or text in _SAFE_DEVICES:
        return False
    if text.startswith(("/", "~")) or (len(text) > 1 and text[1] == ":"):
        return True
    return ".." in re.split(r"[/\\]", text)


# -- python ---------------------------------------------------------------------------------------


def _python(script: CheckScript) -> list[str]:
    try:
        tree = ast.parse(script.body, filename=script.name)
        compile(tree, script.name, "exec")
    except SyntaxError as exc:
        return [f"does not compile: {exc.msg} (line {exc.lineno})"]
    except ValueError as exc:  # a null byte, for one
        return [f"does not compile: {exc}"]
    errors: list[str] = []
    for node in ast.walk(tree):
        errors += _python_node(node)
    return list(dict.fromkeys(errors))


def _python_node(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [e for alias in node.names for e in _module(alias.name)]
    if isinstance(node, ast.ImportFrom):
        return _module(node.module or "")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.strip() in _NETWORK_TOOLS:
            return [f"network: names the network tool {node.value.strip()!r}"]
        return []
    if isinstance(node, ast.Call):
        return _call(node)
    return []


def _module(name: str) -> list[str]:
    top = name.split(".")[0]
    if top == "importlib":
        return ["dynamic import: importlib could load any module, the network included"]
    if top in _NETWORK_MODULES or any(name.startswith(m) for m in _NETWORK_MODULES if "." in m):
        return [f"network: imports {name}"]
    return []


def _const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call(node: ast.Call) -> list[str]:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _DYNAMIC:
        what = "dynamic import" if func.id == "__import__" else "dynamic code"
        return [f"{what}: {func.id}() could run anything, the network included"]
    paths: list[str] = []
    if isinstance(func, ast.Name) and func.id == "open":
        mode = node.args[1] if len(node.args) > 1 else None
        mode = next((k.value for k in node.keywords if k.arg == "mode"), mode)
        text = _const(mode) if mode is not None else "r"
        if node.args and (text is None or _OPEN_WRITE.search(text)):
            paths.append(_const(node.args[0]) or "")
    elif isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        if func.attr in _PATH_WRITES and isinstance(func.value, ast.Call):
            paths += [_const(a) or "" for a in func.value.args]  # Path("..").write_text()
        if (owner == "os" and func.attr in _OS_WRITES) or (
            owner == "shutil" and func.attr in _SHUTIL_WRITES
        ):
            paths += [_const(a) or "" for a in node.args]
    bad = [p for p in paths if outside(p)]
    return [f"writes outside the copy: {p!r}" for p in bad]


# -- shell ----------------------------------------------------------------------------------------


def _shell(body: str) -> list[str]:
    bash = shutil.which("bash")
    if bash is None:
        return ["bash is not available to check the script with bash -n"]
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, parse only (-n), script on stdin
            [bash, "-n"], input=body, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"bash -n could not check the script: {exc}"]
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["syntax error"])[0]
        return [f"bash -n rejects it: {first}"]
    errors: list[str] = []
    if re.search(r"/dev/(tcp|udp)/", body):
        errors.append("network: uses bash's /dev/tcp or /dev/udp")
    for line in body.splitlines():
        errors += _shell_line(line)
    return list(dict.fromkeys(errors))


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.commenters = "#"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: bash -n already accepted it, so split plainly
        return line.split()


def _shell_line(line: str) -> list[str]:
    errors: list[str] = []
    command: str | None = None
    tokens = _tokens(line)
    for i, token in enumerate(tokens):
        if token in _SEPARATORS:
            command = None
            continue
        if token in _REDIRECTS or re.fullmatch(r"\d?>>?", token):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if outside(target):
                errors.append(f"writes outside the copy: redirect to {target!r}")
            continue
        if command is None:
            command = token.rsplit("/", 1)[-1]
            if command in _NETWORK_TOOLS:
                errors.append(f"network: runs {command}")
            continue
        writes = command in _SHELL_WRITES | {"cd", "pushd"} and not token.startswith("-")
        if writes and outside(token):
            errors.append(f"writes outside the copy: {command} {token!r}")
        if command == "dd" and token.startswith("of=") and outside(token[3:]):
            errors.append(f"writes outside the copy: dd {token!r}")
    return errors
