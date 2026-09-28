from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from appmetrica_cli.config import AppMetricaConfig

# 420 is AppMetrica's own "too many requests" code; 429 kept for proxies/gateways.
RETRYABLE = {420, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60.0


class AppMetricaError(Exception):
    def __init__(self, status_code: int, message: str, endpoint: str):
        self.status_code = status_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"AppMetrica {status_code} at {endpoint}: {message}")


class AppMetricaClient:
    def __init__(self, http: httpx.AsyncClient, config: AppMetricaConfig):
        self.http = http
        self.config = config

    # ── Management API ──────────────────────────────────────────────

    async def list_applications(self) -> list[dict]:
        url = f"{self.config.management_base_url}/management/v1/applications"
        resp = await self._request(url)
        return resp.get("applications", [])

    # ── Reporting API ───────────────────────────────────────────────

    async def get_report(
        self,
        app_id: int,
        metrics: list[str],
        dimensions: list[str] | None = None,
        date1: str | None = None,
        date2: str | None = None,
        filters: str | None = None,
        limit: int = 100,
        sort: str | None = None,
    ) -> dict:
        url = f"{self.config.reporting_base_url}/stat/v1/data"
        params: dict = {
            "id": app_id,
            "metrics": ",".join(metrics),
            "lang": self.config.default_lang,
            "limit": limit,
        }
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if date1:
            params["date1"] = date1
        if date2:
            params["date2"] = date2
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort
        return await self._request(url, params=params)

    async def get_drilldown(
        self,
        app_id: int,
        metrics: list[str],
        dimensions: list[str],
        date1: str | None = None,
        date2: str | None = None,
        filters: str | None = None,
        parent_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        url = f"{self.config.reporting_base_url}/stat/v1/data/drilldown"
        params: dict = {
            "id": app_id,
            "metrics": ",".join(metrics),
            "dimensions": ",".join(dimensions),
            "lang": self.config.default_lang,
            "limit": limit,
        }
        if date1:
            params["date1"] = date1
        if date2:
            params["date2"] = date2
        if filters:
            params["filters"] = filters
        if parent_id:
            params["parent_id"] = parent_id
        return await self._request(url, params=params)

    # ── Logs API (async export) ─────────────────────────────────────

    async def export_logs(
        self,
        app_id: int,
        table: str,
        fields: list[str],
        date_since: str,
        date_until: str,
    ) -> list[dict]:
        url = f"{self.config.reporting_base_url}/logs/v1/export/{table}.json"
        params = {
            "application_id": app_id,
            "date_since": date_since,
            "date_until": date_until,
            "fields": ",".join(fields),
        }
        resp = await self._poll_logs(url, params)
        return resp.json().get("data", [])

    async def export_logs_csv(
        self,
        app_id: int,
        table: str,
        fields: list[str],
        date_since: str,
        date_until: str,
        date_dimension: str = "receive",
        progress: Callable[[int], None] | None = None,
    ) -> str:
        """Export one window as CSV text.

        date_since/date_until are "YYYY-MM-DD HH:MM:SS" in the app's time zone (E-032).
        progress, if given, is called with elapsed seconds on every 202 poll.
        """
        url = f"{self.config.reporting_base_url}/logs/v1/export/{table}.csv"
        params = {
            "application_id": app_id,
            "date_since": date_since,
            "date_until": date_until,
            "date_dimension": date_dimension,
            "skip_unavailable_shards": "false",
            "use_utf8_bom": "false",
            "fields": ",".join(fields),
        }
        resp = await self._poll_logs(url, params, progress)
        return resp.text

    async def active_exports(self, app_id: int) -> list[dict]:
        """Export requests AppMetrica is still preparing for this app (read-only)."""
        url = f"{self.config.reporting_base_url}/logs/v1/export/get_active_queries"
        data = await self._request(url, params={"application_id": app_id})
        items = data if isinstance(data, list) else []
        return [q for q in items if q.get("status") == 0]

    async def _poll_logs(
        self, url: str, params: dict, progress: Callable[[int], None] | None = None
    ) -> httpx.Response:
        elapsed = 0
        last = ""
        while elapsed < self.config.logs_poll_timeout:
            resp = await self._get(url, params)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 202:
                last = resp.text.strip()[:80]
                await asyncio.sleep(self.config.logs_poll_interval)
                elapsed += self.config.logs_poll_interval
                if progress:
                    progress(elapsed)
                continue
            self._raise_error(resp, url)
        raise AppMetricaError(
            408,
            f"Logs API timeout: export not finished within {self.config.logs_poll_timeout}s "
            f"(last status: {last or 'none'}). The export stays queued at AppMetrica.",
            url,
        )

    # ── Internals ───────────────────────────────────────────────────

    async def _request(self, url: str, params: dict | None = None) -> dict:
        resp = await self._get(url, params)
        if resp.status_code == 200:
            return resp.json()
        self._raise_error(resp, url)
        return {}  # unreachable, satisfies type checker

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with exponential backoff on rate limits and 5xx; other codes returned as is."""
        attempt = 0
        while True:
            resp: httpx.Response | None = None
            try:
                resp = await self.http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt >= self.config.max_retries:
                    raise AppMetricaError(0, f"Network error: {type(e).__name__}", url) from None
            else:
                if resp.status_code not in RETRYABLE or attempt >= self.config.max_retries:
                    return resp
            await asyncio.sleep(self._retry_delay(attempt, resp))
            attempt += 1

    def _retry_delay(self, attempt: int, resp: httpx.Response | None) -> float:
        header = resp.headers.get("Retry-After") if resp is not None else None
        if header:
            try:
                return min(float(header), MAX_RETRY_AFTER)
            except ValueError:
                pass
        return self.config.retry_base_delay * (2 ** attempt)

    def _raise_error(self, resp: httpx.Response, url: str) -> None:
        messages = {
            401: "Auth failed. Run `appmetrica auth status` to check the token.",
            403: "Access denied. Token may lack permissions for this app.",
            420: "Rate limit exceeded (30 req/sec or 5000 req/day). Wait and retry.",
            429: "Rate limit exceeded (30 req/sec or 5000 req/day). Wait and retry.",
            400: f"Bad request: {resp.text[:500]}",
        }
        msg = messages.get(resp.status_code, f"HTTP {resp.status_code}: {resp.text[:500]}")
        raise AppMetricaError(resp.status_code, msg, url)
