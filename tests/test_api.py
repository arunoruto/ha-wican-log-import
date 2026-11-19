"""Tests for api.py (no Home Assistant needed)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wican_log_import.api import (
    InvalidFilenameError,
    WiCanClient,
    WiCanConnectionError,
    WiCanNotSupportedError,
    normalize_base_url,
    parse_log_listing,
    validate_log_filename,
)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.168.1.118", "http://192.168.1.118"),
        ("http://192.168.1.118/", "http://192.168.1.118"),
        (" wican_0123456789ab.local ", "http://wican_0123456789ab.local"),
        ("10.0.0.5:8080", "http://10.0.0.5:8080"),
    ],
)
def test_normalize_base_url(address: str, expected: str) -> None:
    assert normalize_base_url(address) == expected


@pytest.mark.parametrize("bad", ["", "a/b.db", "..\\x", "../etc", "x\x00"])
def test_validate_filename_rejects(bad: str) -> None:
    with pytest.raises(InvalidFilenameError):
        validate_log_filename(bad)


def test_parse_listing() -> None:
    listing = parse_log_listing(
        {
            "current_db": "b.db",
            "databases": [
                {"filename": "a.db", "created": "2026-11-17", "size": "123", "status": "closed"},
                {"filename": "b.db", "size": 5},
                {"nofilename": True},
            ],
        }
    )
    assert listing.current_db == "b.db"
    assert [(d.filename, d.size) for d in listing.databases] == [("a.db", 123), ("b.db", 5)]


async def test_client_against_fake_device(aioclient_mock: AiohttpClientMocker) -> None:
    base = "http://192.168.1.118"
    aioclient_mock.get(f"{base}/check_status", json={"ecu_status": "online"})
    aioclient_mock.get(
        f"{base}/obd_logs",
        json={"current_db": "x y.db", "databases": [{"filename": "x y.db", "size": 3}]},
    )
    aioclient_mock.get(f"{base}/obd_logs/x%20y.db", content=b"abc")
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    client = WiCanClient(session, "192.168.1.118")
    assert (await client.async_get_status())["ecu_status"] == "online"
    listing = await client.async_list_logs()
    assert listing.databases[0].filename == "x y.db"
    assert await client.async_download_log("x y.db") == b"abc"
    with pytest.raises(InvalidFilenameError):
        await client.async_download_log("../x")
    await session.close()


async def test_client_errors(aioclient_mock: AiohttpClientMocker) -> None:
    base = "http://192.168.1.118"
    aioclient_mock.get(f"{base}/obd_logs", status=404)
    aioclient_mock.get(f"{base}/check_status", exc=aiohttp.ClientConnectionError())
    aioclient_mock.get(f"{base}/obd_logs/a.db", exc=TimeoutError())
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    client = WiCanClient(session, base)
    with pytest.raises(WiCanNotSupportedError):
        await client.async_list_logs()
    with pytest.raises(WiCanConnectionError):
        await client.async_get_status()
    with pytest.raises(WiCanConnectionError):
        await client.async_download_log("a.db")
    await session.close()
