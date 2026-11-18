"""Static validation of panel-written check scripts, before anything runs (Harness²'s
validation-before-rollout): a check that does not compile, reaches for the network, writes
outside its directory or is too big is dropped with a reason."""

from __future__ import annotations

import pytest

from fusion.verify.synthesis import CheckScript
from fusion.verify.validate import MAX_CHECK_BYTES, validate_check


def py(body: str, name: str = "repro_sum", kind: str = "repro") -> CheckScript:
    return CheckScript(name=name, language="python", body=body, kind=kind)  # type: ignore[arg-type]


def sh(body: str, name: str = "repro_sum") -> CheckScript:
    return CheckScript(name=name, language="shell", body=body, kind="repro")


GOOD_PY = "from calc import add\nassert add(2, 3) == 5, add(2, 3)\n"


def test_a_plain_python_check_is_valid() -> None:
    assert validate_check(py(GOOD_PY)) == []


def test_a_plain_shell_check_is_valid() -> None:
    assert validate_check(sh('test "$(cat calc.py | grep -c return)" = 1\n')) == []


def test_a_python_check_that_does_not_compile_is_rejected() -> None:
    errors = validate_check(py("def broken(:\n    pass\n"))
    assert errors and "compile" in errors[0]


def test_a_shell_check_that_does_not_parse_is_rejected() -> None:
    errors = validate_check(sh("if [ 1 = 1 ]; then\n  echo hi\n"))
    assert errors and "bash -n" in errors[0]


@pytest.mark.parametrize(
    "body",
    [
        "import socket\nsocket.create_connection(('example.com', 80))\n",
        "from urllib.request import urlopen\nurlopen('http://example.com')\n",
        "import http.client\n",
        "import requests\n",
        "import httpx\n",
        "s = __import__('so' + 'cket')\n",
        "import importlib\nimportlib.import_module('socket')\n",
        "import subprocess\nsubprocess.run(['curl', 'http://example.com'])\n",
    ],
)
def test_python_network_access_is_rejected(body: str) -> None:
    errors = validate_check(py(body))
    assert errors, body
    assert any("network" in e or "dynamic import" in e for e in errors)


@pytest.mark.parametrize(
    "body",
    [
        "curl -s http://example.com\n",
        "wget http://example.com\n",
        "exec 3<>/dev/tcp/example.com/80\n",
        "nc example.com 80 < /dev/null\n",
    ],
)
def test_shell_network_access_is_rejected(body: str) -> None:
    errors = validate_check(sh(body))
    assert any("network" in e for e in errors), body


@pytest.mark.parametrize(
    "body",
    [
        "open('/etc/passwd-copy', 'w').write('x')\n",
        "from pathlib import Path\nPath('../outside.txt').write_text('x')\n",
        "import os\nos.remove('~/.bashrc')\n",
    ],
)
def test_python_writes_outside_the_copy_are_rejected(body: str) -> None:
    errors = validate_check(py(body))
    assert any("outside" in e for e in errors), body


@pytest.mark.parametrize(
    "body",
    ["echo x > /tmp/escape\n", "cp calc.py ../calc.py\n", "rm -rf ~/project\n", "cd /\n"],
)
def test_shell_writes_outside_the_copy_are_rejected(body: str) -> None:
    errors = validate_check(sh(body))
    assert any("outside" in e for e in errors), body


def test_dev_null_is_not_outside() -> None:
    assert validate_check(sh("python3 -c 'print(1)' > /dev/null\n")) == []


def test_a_huge_check_is_rejected_before_it_is_parsed() -> None:
    errors = validate_check(py("x = 1\n" * (MAX_CHECK_BYTES // 6 + 10)))
    assert errors and "too big" in errors[0]


def test_an_unsafe_name_is_rejected() -> None:
    errors = validate_check(py(GOOD_PY, name="../evil"))
    assert any("name" in e for e in errors)
