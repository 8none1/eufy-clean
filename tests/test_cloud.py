"""Tests for EufyLogin device discovery, including cloud API fallback."""

from unittest.mock import AsyncMock

import pytest

from custom_components.robovac_mqtt.api.cloud import EufyLogin


def _make_login():
    login = EufyLogin("test@test.com", "pass", "udid")
    return login


@pytest.mark.asyncio
async def test_get_devices_cloud_fallback():
    """When AIOT returns empty, fall back to cloud device list."""
    login = _make_login()
    login.eufyApi.get_cloud_device_list = AsyncMock(return_value=[
        {
            "id": "device_abc123",
            "alias_name": "Upstairs Clean",
            "product": {"product_code": "T2262EV", "name": "X8"},
            "software_version": "2.5.18",
        }
    ])
    login.eufyApi.get_device_list = AsyncMock(return_value=[])

    await login.getDevices()

    assert len(login.mqtt_devices) == 1
    d = login.mqtt_devices[0]
    assert d["deviceId"] == "device_abc123"
    assert d["deviceModel"] == "T2262"  # product_code[:5] strips variant suffix
    assert d["deviceName"] == "Upstairs Clean"
    assert d["apiType"] == "novel"
    assert d["dps"] == {}
    assert d["softVersion"] == "2.5.18"


@pytest.mark.asyncio
async def test_get_devices_cloud_fallback_multiple():
    """Fallback returns all cloud devices, not just the first."""
    login = _make_login()
    login.eufyApi.get_cloud_device_list = AsyncMock(return_value=[
        {"id": "dev1", "alias_name": "Upstairs", "product": {"product_code": "T2262", "name": "X8"}, "software_version": "2.5.18"},
        {"id": "dev2", "alias_name": "Downstairs", "product": {"product_code": "T2262", "name": "X8"}, "software_version": "1.6.107"},
    ])
    login.eufyApi.get_device_list = AsyncMock(return_value=[])

    await login.getDevices()

    assert len(login.mqtt_devices) == 2
    assert login.mqtt_devices[0]["deviceId"] == "dev1"
    assert login.mqtt_devices[1]["deviceId"] == "dev2"


@pytest.mark.asyncio
async def test_get_devices_both_empty():
    """When both APIs are empty, no devices are registered."""
    login = _make_login()
    login.eufyApi.get_cloud_device_list = AsyncMock(return_value=[])
    login.eufyApi.get_device_list = AsyncMock(return_value=[])

    await login.getDevices()

    assert login.mqtt_devices == []


@pytest.mark.asyncio
async def test_get_devices_aiot_unmatched_skipped():
    """AIOT devices not found in cloud list are marked invalid and skipped."""
    login = _make_login()
    login.eufyApi.get_cloud_device_list = AsyncMock(return_value=[])
    login.eufyApi.get_device_list = AsyncMock(return_value=[
        {"device_sn": "unknown_sn", "dps": {}}
    ])

    await login.getDevices()

    assert login.mqtt_devices == []


@pytest.mark.asyncio
async def test_get_devices_aiot_matched():
    """When AIOT has devices that match cloud list, standard path is used."""
    login = _make_login()
    login.eufyApi.get_cloud_device_list = AsyncMock(return_value=[
        {"id": "sn123", "alias_name": "My Vac", "product": {"product_code": "T2262", "name": "X8"}, "software_version": "1.0"},
    ])
    login.eufyApi.get_device_list = AsyncMock(return_value=[
        {"device_sn": "sn123", "dps": {"152": "somedata"}, "main_sw_version": "2.0"},
    ])

    await login.getDevices()

    assert len(login.mqtt_devices) == 1
    d = login.mqtt_devices[0]
    assert d["deviceId"] == "sn123"
    assert d["deviceName"] == "My Vac"
    assert d["softVersion"] == "2.0"
    assert d["dps"] == {"152": "somedata"}
    assert d["apiType"] == "novel"  # DPS key 152 is in DPS_MAP
