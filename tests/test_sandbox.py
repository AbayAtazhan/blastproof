"""Integration tests: real containers, real limits."""

import asyncio

import pytest

from app.config import settings
from app.sandbox import DockerSandbox
from tests.conftest import docker

pytestmark = docker


def run(code, **kw):
    return asyncio.run(DockerSandbox(settings).run(code, purpose="test", **kw))


def test_happy_path_returns_stdout_and_chart():
    r = run("import matplotlib.pyplot as plt\nprint('hello', 6*7)\nplt.plot([1, 2, 3])")
    assert r.ok and "hello 42" in r.stdout
    assert [a.name for a in r.artifacts] == ["figure_1.png"]
    assert r.containment == []


def test_exception_is_reported_with_traceback():
    r = run("x = 1 / 0")
    assert not r.ok and r.error_type == "ZeroDivisionError" and "ZeroDivisionError" in r.error


def test_no_network():
    r = run("import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)")
    assert not r.ok
    assert "network_blocked" in {c.kind for c in r.containment}


def test_environment_has_no_secrets():
    r = run("import os\nprint(sorted(os.environ))")
    names = r.stdout
    for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "VULTR"):
        assert word not in names


def test_root_filesystem_is_read_only_and_tmp_is_writable():
    r = run("open('/tmp/ok.txt', 'w').write('x')\nprint('tmp ok')\nopen('/usr/local/x', 'w')")
    assert "tmp ok" in r.stdout and not r.ok


def test_soft_timeout():
    r = run("import time\ntime.sleep(30)", timeout_s=2)
    assert r.timed_out and "timeout" in {c.kind for c in r.containment}


def test_nothing_persists_between_runs():
    run("open('/tmp/state.txt', 'w').write('x')")
    r = run("import os\nprint(os.path.exists('/tmp/state.txt'))")
    assert r.stdout.strip() == "False"
