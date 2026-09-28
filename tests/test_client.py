from __future__ import annotations

import pytest
import httpx
import respx

from appmetrica_cli.client import AppMetricaClient, AppMetricaError
from appmetrica_cli.config import AppMetricaConfig


REPORT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"


@pytest.fixture
def client():
    config = AppMetricaConfig(oauth_token="test-token", retry_base_delay=0)
    http = httpx.AsyncClient(
        headers={"Authorization": "OAuth test-token"},
        timeout=httpx.Timeout(5),
    )
    return AppMetricaClient(http, config)


@respx.mock
@pytest.mark.asyncio
async def test_list_applications(client):
    respx.get("https://api.appmetrica.yandex.com/management/v1/applications").respond(
        json={
            "applications": [
                {"id": 123, "name": "My Game", "bundle_id": "com.example.game"},
                {"id": 456, "name": "Test App", "bundle_id": "com.example.test"},
            ]
        }
    )

    apps = await client.list_applications()
    assert len(apps) == 2
    assert apps[0]["id"] == 123
    assert apps[1]["name"] == "Test App"


@respx.mock
@pytest.mark.asyncio
async def test_get_report(client):
    respx.get(REPORT_URL).respond(
        json={
            "query": {"ids": [123], "metrics": ["ym:ge:users"]},
            "data": [
                {
                    "dimensions": [{"name": "2026-05-10"}],
                    "metrics": [1500],
                }
            ],
            "total_rows": 1,
            "totals": [1500],
        }
    )

    result = await client.get_report(
        app_id=123,
        metrics=["ym:ge:users"],
        dimensions=["ym:ge:date"],
        date1="2026-05-10",
        date2="2026-05-10",
    )
    assert result["total_rows"] == 1
    assert result["data"][0]["metrics"] == [1500]


@respx.mock
@pytest.mark.asyncio
async def test_get_report_with_filters(client):
    route = respx.get(REPORT_URL).respond(
        json={"query": {}, "data": [], "total_rows": 0}
    )

    await client.get_report(
        app_id=123,
        metrics=["ym:ge:users"],
        filters="ym:ge:regionCountryName=='US'",
        sort="-ym:ge:users",
        limit=50,
    )

    request = route.calls.last.request
    assert "filters" in str(request.url)
    assert "sort" in str(request.url)


@respx.mock
@pytest.mark.asyncio
async def test_auth_error(client):
    respx.get("https://api.appmetrica.yandex.com/management/v1/applications").respond(
        status_code=401, text="Unauthorized"
    )

    with pytest.raises(AppMetricaError) as exc_info:
        await client.list_applications()
    assert exc_info.value.status_code == 401
    assert "Auth failed" in exc_info.value.message


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [420, 429])
async def test_rate_limit_error_after_retries(client, status):
    route = respx.get(REPORT_URL).respond(status_code=status, text="Too many requests")

    with pytest.raises(AppMetricaError) as exc_info:
        await client.get_report(123, ["ym:ge:users"])
    assert exc_info.value.status_code == status
    assert "Rate limit" in exc_info.value.message
    assert route.call_count == client.config.max_retries + 1


@respx.mock
@pytest.mark.parametrize("status", [420, 503])
async def test_retry_then_success(client, status):
    route = respx.get(REPORT_URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json={"data": []})]
    )
    assert await client.get_report(123, ["ym:ge:users"]) == {"data": []}
    assert route.call_count == 2


@respx.mock
async def test_network_error_retried_then_reported(client):
    route = respx.get(REPORT_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AppMetricaError) as exc_info:
        await client.get_report(123, ["ym:ge:users"])
    assert exc_info.value.status_code == 0
    assert route.call_count == client.config.max_retries + 1


@respx.mock
async def test_bad_request_not_retried(client):
    route = respx.get(REPORT_URL).respond(status_code=400, text="bad metric")
    with pytest.raises(AppMetricaError):
        await client.get_report(123, ["ym:ge:users"])
    assert route.call_count == 1


def test_retry_after_header_is_capped(client):
    resp = httpx.Response(420, headers={"Retry-After": "3600"})
    assert client._retry_delay(0, resp) == 60.0
    assert client._retry_delay(2, None) == 0  # base delay 0 in tests


@respx.mock
async def test_active_exports_keeps_unfinished_only(client):
    respx.get("https://api.appmetrica.yandex.ru/logs/v1/export/get_active_queries").respond(
        json=[{"request_id": 1, "status": 0}, {"request_id": 2, "status": 1}]
    )
    assert await client.active_exports(123) == [{"request_id": 1, "status": 0}]


@respx.mock
async def test_stuck_export_timeout_message(client):
    client.config = AppMetricaConfig(
        oauth_token="test-token", logs_poll_interval=1, logs_poll_timeout=1, retry_base_delay=0
    )
    respx.get("https://api.appmetrica.yandex.ru/logs/v1/export/events.csv").respond(
        status_code=202, text="Wait for result. Progress is 99%."
    )
    with pytest.raises(AppMetricaError) as e:
        await client.export_logs_csv(123, "events", ["a"], "2026-09-15 00:00:00", "2026-09-15 23:59:59")
    assert e.value.status_code == 408
    assert "Progress is 99%" in e.value.message and "stays queued" in e.value.message


@respx.mock
async def test_drilldown_endpoint(client):
    route = respx.get("https://api.appmetrica.yandex.ru/stat/v1/data/drilldown").respond(
        json={"data": []}
    )
    await client.get_drilldown(123, ["ym:ge:users"], ["ym:ge:regionCountry"])
    assert route.called


@respx.mock
async def test_export_logs_csv_params_and_progress(client):
    client.config = AppMetricaConfig(
        oauth_token="test-token", logs_poll_interval=0, logs_poll_timeout=5, retry_base_delay=0
    )
    route = respx.get("https://api.appmetrica.yandex.ru/logs/v1/export/events.csv").mock(
        side_effect=[httpx.Response(202), httpx.Response(420), httpx.Response(200, text="a,b\n1,2\n")]
    )
    seen: list[int] = []
    text = await client.export_logs_csv(
        123, "events", ["a", "b"], "2026-09-15 00:00:00", "2026-09-15 23:59:59",
        progress=seen.append,
    )
    assert text == "a,b\n1,2\n"
    assert route.call_count == 3
    assert seen == [0]
    params = route.calls.last.request.url.params
    assert params["date_dimension"] == "receive"
    assert params["skip_unavailable_shards"] == "false"
    assert params["date_since"] == "2026-09-15 00:00:00"
    assert params["fields"] == "a,b"


@respx.mock
@pytest.mark.asyncio
async def test_logs_api_polling(client):
    client.config = AppMetricaConfig(
        oauth_token="test-token",
        logs_poll_interval=0,
        logs_poll_timeout=5,
    )

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(202)
        return httpx.Response(200, json={"data": [{"event_name": "test"}]})

    respx.get("https://api.appmetrica.yandex.ru/logs/v1/export/events.json").mock(
        side_effect=side_effect
    )

    result = await client.export_logs(
        app_id=123,
        table="events",
        fields=["event_name"],
        date_since="2026-05-01",
        date_until="2026-05-10",
    )
    assert call_count == 3
    assert result == [{"event_name": "test"}]


@respx.mock
@pytest.mark.asyncio
async def test_logs_api_timeout(client):
    client.config = AppMetricaConfig(
        oauth_token="test-token",
        logs_poll_interval=0,
        logs_poll_timeout=0,
    )

    respx.get("https://api.appmetrica.yandex.ru/logs/v1/export/events.json").respond(
        status_code=202
    )

    with pytest.raises(AppMetricaError) as exc_info:
        await client.export_logs(123, "events", ["event_name"], "2026-05-01", "2026-05-10")
    assert exc_info.value.status_code == 408
    assert "timeout" in exc_info.value.message.lower()
