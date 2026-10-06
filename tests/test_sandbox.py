"""The sandbox: limits, a scrubbed environment, paths that stay inside, and honest isolation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import fusion.bench.sandbox as sandbox_module
from fusion.bench.sandbox import (
    ENV_ALLOWLIST,
    ISOLATION_ENV,
    IsolationUnavailableError,
    Sandbox,
    SandboxError,
    SandboxLimits,
    default_isolation,
    detect_isolation,
    safe_relative_path,
)

needs_isolation = pytest.mark.skipif(
    detect_isolation() == "none", reason="no sandbox-exec or unshare on this machine"
)


def test_files_are_copied_in_run_and_collected() -> None:
    with Sandbox() as box:
        box.copy_in(
            {"pkg/mod.py": "VALUE = 41\n", "main.py": "import pkg.mod\nprint(pkg.mod.VALUE + 1)\n"}
        )
        result = box.run(["python", "main.py"])
        assert result.ok and result.stdout.strip() == "42"
        box.run(["python", "-c", "open('out/result.txt', 'w').write('x')"])
        box.write("out/seed.txt", "seed")
        assert box.collect(["out"]) == {"out/seed.txt": b"seed"}
        assert box.collect(["main.py", "missing.py"]) == {"main.py": box.read("main.py")}


def test_the_directory_is_removed_on_exit() -> None:
    with Sandbox() as box:
        root = box.root
        box.write("a.txt", "x")
        assert root.exists()
    assert not root.exists()
    with pytest.raises(SandboxError, match="context manager"):
        _ = box.root


def test_a_failing_command_is_a_result_not_an_exception() -> None:
    with Sandbox() as box:
        result = box.run(
            ["python", "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"]
        )
        assert (result.returncode, result.ok) == (3, False)
        assert result.stdout.strip() == "out" and result.stderr == "err"
        assert box.run(["definitely-not-a-command-xyz"]).returncode != 0
        with pytest.raises(SandboxError):
            box.run([])


def test_a_command_that_runs_too_long_is_killed() -> None:
    with Sandbox() as box:
        result = box.run(["python", "-c", "while True: pass"], timeout=1)
        assert result.timed_out and result.returncode is None and not result.ok
        assert result.seconds < 10


def test_children_are_killed_with_the_command() -> None:
    with Sandbox() as box:
        marker = "sleeper-" + box.root.name
        box.write(
            "run.py",
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # {marker}'])\n"
            "time.sleep(60)\n",
        )
        result = box.run(["python", "run.py"], timeout=1)
        assert result.timed_out
        import subprocess

        left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
        assert left.stdout.strip() == ""


def test_output_is_capped() -> None:
    with Sandbox(limits=SandboxLimits(max_output_bytes=1000)) as box:
        result = box.run(["python", "-c", "print('x' * 50000)"])
        assert result.truncated and len(result.stdout) == 1000


def test_a_file_larger_than_the_limit_cannot_be_written() -> None:
    with Sandbox(limits=SandboxLimits(max_file_mb=1)) as box:
        result = box.run(["python", "-c", "open('big.bin', 'wb').write(b'x' * 5_000_000)"])
        assert not result.ok
        assert len(box.collect(["big.bin"]).get("big.bin", b"")) <= 1024 * 1024


def test_the_environment_is_scrubbed_of_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "MY_SERVICE_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, "super-secret-value")
    with Sandbox() as box:
        result = box.run(["python", "-c", "import os; print(sorted(os.environ))"])
        assert "super-secret-value" not in result.output
        names = ast.literal_eval(result.stdout.strip())
        assert not {n for n in names if "KEY" in n or "TOKEN" in n or "SECRET" in n}
        allowed = {*ENV_ALLOWLIST, "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED"}
        assert {"HOME", "TMPDIR"} <= set(names)
        assert (
            set(names)
            - allowed
            - {"PYTHONUNBUFFERED", "PYTHONNOUSERSITE", "__CF_USER_TEXT_ENCODING"}
            == set()
        )


def test_home_and_tmp_point_inside_the_sandbox() -> None:
    with Sandbox() as box:
        result = box.run(
            [
                "python",
                "-c",
                "import os, tempfile; print(os.environ['HOME']); print(tempfile.gettempdir())",
            ]
        )
        home, tmp = result.stdout.split()
        assert Path(home).parent == box.root and Path(tmp).parent == box.root


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../x",
        "a/../../x",
        "/etc/passwd",
        "~/x",
        "C:/x",
        "a\\b",
        ".git/config",
        "a/.git/x",
        "a\x00b",
        "../" * 30,
    ],
)
def test_paths_that_could_leave_the_sandbox_are_rejected(bad: str) -> None:
    with pytest.raises(SandboxError):
        safe_relative_path(bad)
    with Sandbox() as box, pytest.raises(SandboxError):
        box.write(bad, "x")


def test_safe_relative_path_cleans_dots() -> None:
    assert safe_relative_path("./a//b/./c.py".replace("//", "/")) == "a/b/c.py"


def test_a_symlink_that_leaves_the_sandbox_is_never_followed() -> None:
    with Sandbox() as box:
        box.run(
            [
                "python",
                "-c",
                "import os; os.symlink('/etc/hosts', 'link.txt'); os.symlink('/etc', 'dir')",
            ]
        )
        assert box.collect(["link.txt", "dir"]) == {}
        with pytest.raises(SandboxError):
            box.read("link.txt")
        with pytest.raises(SandboxError):
            box.write("dir/evil.txt", "x")


def test_copy_in_takes_a_directory(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    with Sandbox() as box:
        box.copy_in(tmp_path, dest="project")
        assert box.read("project/src/a.py") == b"A = 1\n"


@needs_isolation
def test_the_network_and_writes_outside_the_sandbox_are_blocked(tmp_path: Path) -> None:
    target = tmp_path / "escaped.txt"
    with Sandbox(isolation="require") as box:
        net = box.run(
            ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)"]
        )
        assert not net.ok
        out = box.run(["python", "-c", f"open({str(target)!r}, 'w').write('x')"])
        assert not out.ok and not target.exists()
        assert box.run(["python", "-c", "open('inside.txt', 'w').write('x')"]).ok


def test_isolation_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_module, "detect_isolation", lambda: "none")
    with pytest.raises(IsolationUnavailableError, match="isolation"):
        Sandbox(isolation="require").__enter__()
    with Sandbox(isolation="auto") as box:  # runs, with the limits only, and says so
        assert box.isolation == "none"
        assert box.run([sys.executable, "-c", "print(1)"]).ok
    monkeypatch.setattr(
        sandbox_module, "detect_isolation", lambda: pytest.fail("off must not probe")
    )
    with Sandbox(isolation="off") as box:
        assert box.isolation == "none"


def test_the_isolation_mode_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_isolation() == "auto"
    monkeypatch.setenv(ISOLATION_ENV, "REQUIRE")
    assert default_isolation() == "require"
    monkeypatch.setenv(ISOLATION_ENV, "nonsense")
    assert default_isolation() == "auto"
