"""AppMetrica OAuth token: OS credential store, env var, or a protected token file.

Security rules (plan §8):
- The OS store is pinned per platform (macOS Keychain, Windows Credential Manager,
  Linux Secret Service). keyring's global backend selection (PYTHON_KEYRING_BACKEND,
  keyringrc.cfg) is never used, and any KEYRING_PROPERTY_* variable is refused,
  because those can silently redirect where the token is written.
- The tool never writes the token in plaintext anywhere.
- Error messages never contain the token: only fixed text, variable names, paths
  and exception class names.
"""
from __future__ import annotations

import importlib
import os
import stat
import sys
from dataclasses import dataclass
from typing import Any

from keyring.errors import PasswordDeleteError

SERVICE = "appmetrica-cli"
ACCOUNT = "default"
ENV_VAR = "APPMETRICA_OAUTH_TOKEN"
FILE_ENV_VAR = "APPMETRICA_OAUTH_TOKEN_FILE"
PROPERTY_PREFIX = "KEYRING_PROPERTY_"
MAX_TOKEN_LEN = 1024  # 2048 bytes as UTF-16, under the Windows credential blob limit
MAX_FILE_BYTES = 4096

EXIT_API_REJECTED = 2
EXIT_STORE = 3
EXIT_CONFIG = 4

# platform -> (source name, module, class)
_BACKENDS: dict[str, tuple[str, str, str]] = {
    "darwin": ("keychain", "keyring.backends.macOS", "Keyring"),
    "win32": ("credential-manager", "keyring.backends.Windows", "WinVaultKeyring"),
    "linux": ("secret-service", "keyring.backends.SecretService", "Keyring"),
}

STORE_UNAVAILABLE = (
    "No OS credential store is available (macOS Keychain, Windows Credential Manager "
    f"or Linux Secret Service). On a server or in CI, set {ENV_VAR}, or put the token "
    f"in a file readable only by you (chmod 600) and set {FILE_ENV_VAR} to its path."
)


class AuthError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_STORE):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


# ── validation ──────────────────────────────────────────────────────


def validate_token(raw: str) -> str:
    token = raw.strip()
    if not 1 <= len(token) <= MAX_TOKEN_LEN or any(not 0x21 <= ord(c) <= 0x7E for c in token):
        raise AuthError(
            f"Invalid token format: expected 1-{MAX_TOKEN_LEN} printable ASCII characters "
            "without spaces",
            EXIT_CONFIG,
        )
    return token


def token_from_bytes(data: bytes, origin: str) -> str:
    """Decode raw bytes (stdin, token file) strictly as ASCII, then validate."""
    if len(data) > MAX_FILE_BYTES:
        raise AuthError(f"{origin} is larger than {MAX_FILE_BYTES} bytes", EXIT_CONFIG)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise AuthError(f"{origin} contains non-ASCII bytes", EXIT_CONFIG) from None
    return validate_token(text)


# ── OS credential store ─────────────────────────────────────────────


@dataclass
class Store:
    name: str
    backend: Any

    def _fail(self, e: BaseException) -> AuthError:
        return AuthError(f"{self.name} error: {type(e).__name__}", EXIT_STORE)

    def get(self) -> str | None:
        try:
            return self.backend.get_password(SERVICE, ACCOUNT)
        except Exception as e:
            raise self._fail(e) from None

    def set(self, token: str) -> None:
        try:
            self.backend.set_password(SERVICE, ACCOUNT, token)
        except Exception as e:
            raise self._fail(e) from None

    def delete(self) -> bool:
        """True if an item was deleted, False if there was none."""
        try:
            self.backend.delete_password(SERVICE, ACCOUNT)
            return True
        except PasswordDeleteError as e:
            # "No such item" on every backend; on macOS it also wraps real errors,
            # so read once more to tell them apart.
            if self.get() is None:
                return False
            raise self._fail(e) from None
        except Exception as e:
            raise self._fail(e) from None


def _load_class(module: str, name: str) -> type:
    return getattr(importlib.import_module(module), name)


def _platform_key() -> str | None:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def _store() -> Store | None:
    """The pinned OS store for this platform, or None if it is not usable here."""
    redirect = sorted(k for k in os.environ if k.startswith(PROPERTY_PREFIX))
    if redirect:
        raise AuthError(
            f"Refusing to use the credential store: {redirect[0]} is set and could redirect "
            "where the token is stored. Unset it and retry.",
            EXIT_CONFIG,
        )
    key = _platform_key()
    if key is None:
        return None
    name, module, cls_name = _BACKENDS[key]
    try:
        cls = _load_class(module, cls_name)
        if not cls.viable:
            return None
        backend = cls()
    except Exception:
        return None
    if key == "win32":
        backend.persist = "local machine"  # default ENTERPRISE roams with the domain profile
    return Store(name, backend)


def require_store() -> Store:
    store = _store()
    if store is None:
        raise AuthError(STORE_UNAVAILABLE, EXIT_STORE)
    return store


# ── token file ──────────────────────────────────────────────────────


def _read_token_file(path: str) -> str:
    if sys.platform == "win32":
        raise AuthError(
            f"{FILE_ENV_VAR} is not supported on Windows; use `appmetrica auth login` "
            f"(Credential Manager) or {ENV_VAR}",
            EXIT_CONFIG,
        )
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise AuthError(f"{FILE_ENV_VAR}: cannot open {path} ({type(e).__name__})", EXIT_CONFIG) from None
    try:
        # All checks on the descriptor that is actually read (no stat-then-open race).
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthError(f"{FILE_ENV_VAR}: {path} is not a regular file", EXIT_CONFIG)
        if st.st_uid != os.geteuid():
            raise AuthError(f"{FILE_ENV_VAR}: {path} must be owned by the current user", EXIT_CONFIG)
        if st.st_mode & 0o077:
            raise AuthError(
                f"{FILE_ENV_VAR}: {path} is accessible by other users; run: chmod 600 {path}",
                EXIT_CONFIG,
            )
        if st.st_size > MAX_FILE_BYTES:
            raise AuthError(f"{FILE_ENV_VAR}: {path} is larger than {MAX_FILE_BYTES} bytes", EXIT_CONFIG)
        chunks = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return token_from_bytes(b"".join(chunks), f"{FILE_ENV_VAR}: {path}")


# ── public API ──────────────────────────────────────────────────────


def get_token() -> tuple[str | None, str]:
    """Return (token, source). source: env, file, keychain, credential-manager,
    secret-service or none. Raises AuthError on misconfiguration or a store failure."""
    env = os.environ.get(ENV_VAR)
    path = os.environ.get(FILE_ENV_VAR)
    if env and path:
        raise AuthError(f"Both {ENV_VAR} and {FILE_ENV_VAR} are set; keep only one", EXIT_CONFIG)
    if env:
        return validate_token(env), "env"
    if path:
        return _read_token_file(path), "file"
    store = require_store()
    token = store.get()
    if token is None:
        return None, "none"
    return validate_token(token), store.name


def save_token(token: str) -> str:
    """Validate and store in the OS store; returns the store name."""
    token = validate_token(token)
    store = require_store()
    store.set(token)
    return store.name


def delete_token() -> bool:
    return require_store().delete()


def external_source() -> str | None:
    """Name of the env var that supplies a token outside the OS store, if any."""
    if os.environ.get(ENV_VAR):
        return ENV_VAR
    if os.environ.get(FILE_ENV_VAR):
        return FILE_ENV_VAR
    return None
