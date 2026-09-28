from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from appmetrica_cli.auth import AuthError, get_token


def home_dir() -> Path:
    return Path(os.environ.get("APPMETRICA_HOME", Path.home() / ".appmetrica-cli"))


@dataclass(frozen=True)
class AppMetricaConfig:
    # repr=False: the token must never appear in logs, errors or reprs
    oauth_token: str = field(repr=False)
    management_base_url: str = "https://api.appmetrica.yandex.com"
    reporting_base_url: str = "https://api.appmetrica.yandex.ru"
    default_lang: str = "en"
    request_timeout: int = 30
    logs_poll_interval: int = 10
    logs_poll_timeout: int = 300
    max_retries: int = 3
    retry_base_delay: float = 1.0
    cache_dir: Path = Path.home() / ".appmetrica_cache"

    @classmethod
    def from_env(cls) -> "AppMetricaConfig":
        token, _source = get_token()
        if not token:
            raise AuthError(
                "No AppMetrica token. Run `appmetrica auth login` "
                "or set APPMETRICA_OAUTH_TOKEN (see docs/setup-guide.md).",
                exit_code=2,
            )
        kwargs: dict = {"oauth_token": token}
        cache = os.environ.get("APPMETRICA_CACHE_DIR")
        if cache:
            kwargs["cache_dir"] = Path(cache)
        return cls(**kwargs)
