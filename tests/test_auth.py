from __future__ import annotations

import os
import sys

import pytest
from click.testing import CliRunner
from keyring.errors import PasswordDeleteError

from appmetrica_cli import auth, cli
from appmetrica_cli.auth import AuthError
from appmetrica_cli.client import AppMetricaError
from appmetrica_cli.config import AppMetricaConfig

SENTINEL = "SENTINEL-oauth-9f3c"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX token file")


class FakeBackend:
    viable = True
    items: dict = {}
    fail_on: set = set()
    created: list = []

    def __init__(self):
        type(self).created.append(self)

    def _maybe_fail(self, op):
        if op in self.fail_on:
            raise RuntimeError(f"backend exploded while handling {SENTINEL}")

    def get_password(self, service, account):
        self._maybe_fail("get")
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self._maybe_fail("set")
        self.items[(service, account)] = value

    def delete_password(self, service, account):
        self._maybe_fail("delete")
        if "delete_denied" in self.fail_on or (service, account) not in self.items:
            raise PasswordDeleteError("No such password!")
        del self.items[(service, account)]


@pytest.fixture
def fake(monkeypatch):
    class Backend(FakeBackend):
        items = {}
        fail_on = set()
        created = []

    loaded = []

    def load(module, name):
        loaded.append((module, name))
        return Backend

    monkeypatch.setattr(auth, "_load_class", load)
    monkeypatch.setattr(auth, "_platform_key", lambda: "darwin")
    for k in list(os.environ):
        if k.startswith(auth.PROPERTY_PREFIX) or k in (auth.ENV_VAR, auth.FILE_ENV_VAR):
            monkeypatch.delenv(k)
    Backend.loaded = loaded
    return Backend


@pytest.fixture
def api(monkeypatch):
    calls = []

    async def accept(token):
        calls.append(token)
        return [{"id": 1}, {"id": 2}]

    monkeypatch.setattr(cli, "_list_apps", accept)
    return calls


def run(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input)


def assert_no_leak(result):
    assert SENTINEL not in result.output
    assert "Traceback" not in result.output


# ── backend pinning ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key, name, module",
    [
        ("darwin", "keychain", "keyring.backends.macOS"),
        ("win32", "credential-manager", "keyring.backends.Windows"),
        ("linux", "secret-service", "keyring.backends.SecretService"),
    ],
)
def test_platform_mapping(fake, monkeypatch, key, name, module):
    monkeypatch.setattr(auth, "_platform_key", lambda: key)
    store = auth.require_store()
    assert store.name == name
    assert fake.loaded[-1][0] == module
    if key == "win32":
        assert store.backend.persist == "local machine"
    else:
        assert not hasattr(store.backend, "persist")


def test_python_keyring_backend_is_ignored(fake, monkeypatch):
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    assert auth.save_token(SENTINEL) == "keychain"
    assert auth.get_token() == (SENTINEL, "keychain")


def test_real_mapping_never_uses_global_keyring(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: pytest.fail("global keyring used"))
    monkeypatch.setattr(auth, "_platform_key", lambda: None)
    assert auth._store() is None


@pytest.mark.parametrize("var", ["KEYRING_PROPERTY_PREFERRED_COLLECTION", "KEYRING_PROPERTY_PERSIST"])
def test_keyring_property_env_fails_closed(fake, api, monkeypatch, var):
    monkeypatch.setenv(var, "/x")
    r = run("auth", "login", "--stdin", input=SENTINEL + "\n")
    assert r.exit_code == auth.EXIT_CONFIG
    assert var in r.output and "/x" not in r.output
    assert fake.items == {} and api == []
    assert_no_leak(r)


# ── no store / store errors ─────────────────────────────────────────


def test_login_without_store_exits_before_api(fake, api):
    fake.viable = False
    r = run("auth", "login", "--stdin", input=SENTINEL + "\n")
    assert r.exit_code == auth.EXIT_STORE
    assert auth.ENV_VAR in r.output and auth.FILE_ENV_VAR in r.output and "chmod 600" in r.output
    assert api == []
    assert_no_leak(r)


def test_get_token_without_store_raises_not_none(fake):
    fake.viable = False
    with pytest.raises(AuthError) as e:
        auth.get_token()
    assert e.value.exit_code == auth.EXIT_STORE


@pytest.mark.parametrize("op, args", [("set", ("auth", "login", "--stdin")), ("get", ("auth", "status")),
                                      ("delete", ("auth", "logout"))])
def test_backend_exception_is_wrapped(fake, api, op, args):
    fake.items[(auth.SERVICE, auth.ACCOUNT)] = SENTINEL
    fake.fail_on = {op}
    r = run(*args, input=SENTINEL + "\n")
    assert r.exit_code == auth.EXIT_STORE
    assert "keychain error: RuntimeError" in r.output
    assert_no_leak(r)


def test_logout_without_item_is_not_an_error(fake):
    r = run("auth", "logout")
    assert r.exit_code == 0
    assert '"deleted": false' in r.output


def test_logout_delete_error_with_item_present_is_store_error(fake):
    fake.items[(auth.SERVICE, auth.ACCOUNT)] = SENTINEL
    fake.fail_on = {"delete_denied"}
    r = run("auth", "logout")
    assert r.exit_code == auth.EXIT_STORE
    assert_no_leak(r)


def test_logout_warns_when_env_still_set(fake, monkeypatch):
    monkeypatch.setenv(auth.ENV_VAR, SENTINEL)
    r = run("auth", "logout")
    assert r.exit_code == 0 and auth.ENV_VAR in r.output
    assert_no_leak(r)


# ── validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["a b", "ab\rcd", "tökén", "x" * 1025, "   ", ""])
def test_validator_rejects_before_network(fake, api, bad):
    r = run("auth", "login", "--stdin", input=bad + "\n")
    assert r.exit_code == auth.EXIT_CONFIG
    assert api == [] and fake.items == {}


@pytest.mark.parametrize("bad", ["a b", "x" * 1025, "tökén"])
def test_validator_applies_to_env(fake, monkeypatch, bad):
    monkeypatch.setenv(auth.ENV_VAR, bad)
    with pytest.raises(AuthError) as e:
        auth.get_token()
    assert e.value.exit_code == auth.EXIT_CONFIG


def test_validator_strips_newlines():
    assert auth.validate_token("  y0_abc\r\n") == "y0_abc"
    assert auth.validate_token("x" * 1024) == "x" * 1024


# ── login / status ──────────────────────────────────────────────────


def test_login_and_status_report_same_source(fake, api):
    r = run("auth", "login", "--stdin", input=SENTINEL + "\n")
    assert r.exit_code == 0, r.output
    assert '"stored_in": "keychain"' in r.output
    assert api == [SENTINEL]
    s = run("auth", "status")
    assert s.exit_code == 0 and '"source": "keychain"' in s.output
    assert_no_leak(r)
    assert_no_leak(s)


def test_login_rejected_token_not_stored(fake, monkeypatch):
    async def reject(token):
        raise AppMetricaError(401, "Unauthorized", "/management/v1/applications")

    monkeypatch.setattr(cli, "_list_apps", reject)
    r = run("auth", "login", "--stdin", input=SENTINEL + "\n")
    assert r.exit_code == auth.EXIT_API_REJECTED
    assert fake.items == {}
    assert_no_leak(r)


def test_status_without_token(fake):
    r = run("auth", "status")
    assert r.exit_code == 2 and '"source": "none"' in r.output


def test_env_takes_precedence_and_both_sources_rejected(fake, monkeypatch, tmp_path):
    auth.save_token("stored")
    monkeypatch.setenv(auth.ENV_VAR, "from-env")
    assert auth.get_token() == ("from-env", "env")
    monkeypatch.setenv(auth.FILE_ENV_VAR, str(tmp_path / "t"))
    with pytest.raises(AuthError) as e:
        auth.get_token()
    assert e.value.exit_code == auth.EXIT_CONFIG


def test_config_hides_token(fake):
    auth.save_token(SENTINEL)
    config = AppMetricaConfig.from_env()
    assert config.oauth_token == SENTINEL
    assert SENTINEL not in repr(config)


# ── token file ──────────────────────────────────────────────────────


def _token_file(tmp_path, content: bytes, mode=0o600):
    p = tmp_path / "token"
    p.write_bytes(content)
    p.chmod(mode)
    return p


def _file_token(monkeypatch, path):
    monkeypatch.setenv(auth.FILE_ENV_VAR, str(path))
    return auth.get_token()


@posix_only
def test_file_0600_ok_and_crlf_stripped(fake, monkeypatch, tmp_path):
    p = _token_file(tmp_path, SENTINEL.encode() + b"\r\n")
    assert _file_token(monkeypatch, p) == (SENTINEL, "file")


@posix_only
def test_file_symlink_to_0600_ok(fake, monkeypatch, tmp_path):
    p = _token_file(tmp_path, SENTINEL.encode())
    link = tmp_path / "link"
    link.symlink_to(p)
    assert _file_token(monkeypatch, link) == (SENTINEL, "file")


@posix_only
def test_file_group_readable_rejected(fake, monkeypatch, tmp_path):
    p = _token_file(tmp_path, SENTINEL.encode(), 0o644)
    with pytest.raises(AuthError) as e:
        _file_token(monkeypatch, p)
    assert "chmod 600" in e.value.message and SENTINEL not in e.value.message
    assert e.value.exit_code == auth.EXIT_CONFIG


@posix_only
def test_file_directory_rejected(fake, monkeypatch, tmp_path):
    d = tmp_path / "dir"
    d.mkdir(mode=0o700)
    with pytest.raises(AuthError, match="not a regular file"):
        _file_token(monkeypatch, d)


@posix_only
def test_file_fifo_rejected_without_blocking(fake, monkeypatch, tmp_path):
    f = tmp_path / "fifo"
    os.mkfifo(f, 0o600)
    with pytest.raises(AuthError, match="not a regular file"):
        _file_token(monkeypatch, f)


@posix_only
def test_file_too_large_rejected(fake, monkeypatch, tmp_path):
    p = _token_file(tmp_path, b"x" * (auth.MAX_FILE_BYTES + 1))
    with pytest.raises(AuthError, match="larger than"):
        _file_token(monkeypatch, p)


@posix_only
def test_file_invalid_content_rejected(fake, monkeypatch, tmp_path):
    p = _token_file(tmp_path, b"two words\n")
    with pytest.raises(AuthError) as e:
        _file_token(monkeypatch, p)
    assert e.value.exit_code == auth.EXIT_CONFIG


@posix_only
def test_file_missing_rejected(fake, monkeypatch, tmp_path):
    with pytest.raises(AuthError, match="cannot open"):
        _file_token(monkeypatch, tmp_path / "nope")
