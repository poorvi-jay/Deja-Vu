"""Configuration loading.

These exist because of a real production failure: load_env() used to exit when
.env was missing, so the deployed service reported "No .env file found" while
the platform's environment variables sat unread. Locally there was always a
.env, so nothing caught it until the service was live.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_cypher import connect, load_env  # noqa: E402


def test_a_missing_env_file_is_not_an_error(tmp_path):
    """The deployed case: no file, real environment variables instead."""
    assert load_env(tmp_path / "does-not-exist") is False


def test_an_existing_env_file_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("DEJAVU_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("DEJAVU_TEST_KEY=from-file\n", encoding="utf-8")

    assert load_env(env) is True
    import os

    assert os.environ["DEJAVU_TEST_KEY"] == "from-file"


def test_real_environment_variables_win_over_the_file(tmp_path, monkeypatch):
    """A value already set by the host must not be overwritten by a file.

    Otherwise a committed default could shadow the real deployed credential.
    """
    monkeypatch.setenv("DEJAVU_TEST_KEY", "from-host")
    env = tmp_path / ".env"
    env.write_text("DEJAVU_TEST_KEY=from-file\n", encoding="utf-8")

    load_env(env)
    import os

    assert os.environ["DEJAVU_TEST_KEY"] == "from-host"


def test_comments_and_blank_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("DEJAVU_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "\n# a comment\n\nDEJAVU_TEST_KEY=value\nnot-a-pair\n", encoding="utf-8"
    )

    load_env(env)
    import os

    assert os.environ["DEJAVU_TEST_KEY"] == "value"


def test_connect_without_any_configuration_says_what_is_missing(monkeypatch):
    """The message has to name env vars, not just .env — on a deployed host
    there is no file to fix."""
    for key in (
        "FALKOR_URL",
        "FALKOR_HOST",
        "FALKOR_PORT",
        "FALKOR_USER",
        "FALKOR_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(SystemExit) as exc:
        connect()
    message = str(exc.value)
    assert "FALKOR_URL" in message
    assert "environment variables" in message


def test_connect_rejects_a_malformed_url(monkeypatch):
    monkeypatch.setenv("FALKOR_URL", "not-a-url")
    with pytest.raises(SystemExit) as exc:
        connect()
    assert "does not look right" in str(exc.value)
