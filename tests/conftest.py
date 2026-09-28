from __future__ import annotations

import pytest
import duckdb

from appmetrica_cli.cache import EventCache, EVENTS_FIELDS, INSTALLATIONS_FIELDS
from appmetrica_cli.config import AppMetricaConfig


@pytest.fixture
def config():
    return AppMetricaConfig(oauth_token="test-token-123")


@pytest.fixture
def cache(tmp_path):
    c = EventCache(tmp_path / "cache", in_memory=True)
    yield c
    c.close_all()


def _make_event(**overrides):
    base = {
        "application_id": 123456,
        "app_build_number": "100",
        "ios_ifa": None,
        "ios_ifv": None,
        "android_id": None,
        "google_aid": None,
        "profile_id": None,
        "os_name": "android",
        "os_version": "15",
        "device_manufacturer": "Samsung",
        "device_model": "Galaxy S24",
        "device_type": "phone",
        "device_locale": "en_US",
        "device_ipv6": None,
        "app_version_name": "1.0.0",
        "app_package_name": "com.example.game",
        "event_name": "test",
        "event_json": None,
        "event_datetime": "2026-05-10 10:00:00",
        "event_timestamp": 1778518800,
        "event_receive_datetime": "2026-05-10 10:00:05",
        "event_receive_timestamp": 1778518805,
        "connection_type": "wifi",
        "operator_name": None,
        "original_device_model": None,
        "mcc": None,
        "mnc": None,
        "country_iso_code": "US",
        "city": "New York",
        "appmetrica_device_id": 1001,
        "installation_id": "inst_1",
        "session_id": 100,
        "windows_aid": None,
    }
    base.update(overrides)
    return base


def _make_install(**overrides):
    base = {
        "application_id": 123456,
        "installation_id": "inst_1",
        "publisher_name": "Facebook Ads",
        "publisher_id": 5,
        "tracker_name": "facebook",
        "tracking_id": 0,
        "click_timestamp": 0,
        "click_datetime": None,
        "click_ipv6": None,
        "click_url_parameters": None,
        "click_id": None,
        "click_user_agent": None,
        "match_type": "referrer",
        "install_datetime": "2026-05-10 09:00:00",
        "install_timestamp": 1778515200,
        "install_receive_datetime": "2026-05-10 09:00:05",
        "install_receive_timestamp": 1778515205,
        "install_ipv6": None,
        "is_reinstallation": False,
        "is_reattribution": False,
        "ios_ifa": None,
        "ios_ifv": None,
        "android_id": None,
        "google_aid": None,
        "profile_id": None,
        "os_name": "android",
        "os_version": "15",
        "oaid": None,
        "device_manufacturer": "Samsung",
        "device_model": "Galaxy S24",
        "device_type": "phone",
        "device_locale": "en_US",
        "app_version_name": "1.0.0",
        "app_package_name": "com.example.game",
        "connection_type": "wifi",
        "operator_name": None,
        "mcc": None,
        "mnc": None,
        "country_iso_code": "US",
        "city": "New York",
        "appmetrica_device_id": 1001,
        "attributed_touch_type": "unknown",
        "windows_aid": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def sample_events():
    return [
        _make_event(
            event_name="tutorial_start",
            event_json='{"step": "1"}',
            event_datetime="2026-05-10 10:00:00",
            appmetrica_device_id=1001,
            installation_id="inst_1",
            session_id=100,
            country_iso_code="US",
            city="New York",
            os_name="ios",
            device_manufacturer="Apple",
            device_model="iPhone 15",
        ),
        _make_event(
            event_name="tutorial_complete",
            event_json='{"step": "5"}',
            event_datetime="2026-05-10 10:05:00",
            appmetrica_device_id=1001,
            installation_id="inst_1",
            session_id=100,
            country_iso_code="US",
            city="New York",
            os_name="ios",
            device_manufacturer="Apple",
            device_model="iPhone 15",
        ),
        _make_event(
            event_name="tutorial_start",
            event_json='{"step": "1"}',
            event_datetime="2026-05-10 11:00:00",
            appmetrica_device_id=1002,
            installation_id="inst_2",
            session_id=200,
            country_iso_code="GB",
            city="London",
        ),
        _make_event(
            event_name="session_start",
            event_json=None,
            event_datetime="2026-05-11 09:00:00",
            appmetrica_device_id=1001,
            installation_id="inst_1",
            session_id=300,
            country_iso_code="US",
            city="New York",
            os_name="ios",
            device_manufacturer="Apple",
            device_model="iPhone 15",
        ),
    ]


@pytest.fixture
def sample_installations():
    return [
        _make_install(
            installation_id="inst_1",
            install_datetime="2026-05-10 09:00:00",
            appmetrica_device_id=1001,
            publisher_name="Facebook Ads",
            tracker_name="facebook",
            country_iso_code="US",
            city="New York",
            os_name="ios",
            device_manufacturer="Apple",
            device_model="iPhone 15",
        ),
        _make_install(
            installation_id="inst_2",
            install_datetime="2026-05-10 10:30:00",
            appmetrica_device_id=1002,
            publisher_name="Applovin",
            tracker_name="applovin",
            country_iso_code="GB",
            city="London",
        ),
    ]
