"""Minimal async HTTP client for the WiCAN Pro log endpoints.

Kept free of Home Assistant imports so it can be ported into other projects
(e.g. the official ``wican`` integration) unchanged. Only ``aiohttp`` is used.

Endpoints (plain HTTP, no auth), as used by wican-cli:

* ``GET /check_status``          JSON status summary (cheap reachability probe)
* ``GET /obd_logs``              JSON ``{"current_db": str, "databases": [...]}``
* ``GET /obd_logs/<filename>``   raw bytes of one SQLite log database
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

DEFAULT_PROBE_TIMEOUT = 4.0
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_DOWNLOAD_TIMEOUT = 120.0
# Refuse absurd downloads instead of exhausting memory on a misbehaving device.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class WiCanError(Exception):
    """Base error for the WiCAN client."""


class WiCanConnectionError(WiCanError):
    """Device is unreachable (asleep, off Wi-Fi, timed out)."""


class WiCanResponseError(WiCanError):
    """Device answered with an error status or an unexpected payload."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.status = status


class WiCanNotSupportedError(WiCanResponseError):
    """The endpoint does not exist on this device (no SD logger support)."""


class InvalidFilenameError(WiCanError):
    """A log filename that cannot be used safely in a URL or path."""


@dataclass(frozen=True, slots=True)
class LogDatabase:
    """One log database as listed by ``/obd_logs``."""

    filename: str
    created: str
    size: int
    status: str


@dataclass(frozen=True, slots=True)
class LogListing:
    """Result of ``/obd_logs``."""

    current_db: str | None
    databases: list[LogDatabase]


def normalize_base_url(address: str) -> str:
    """Turn an IP, hostname or URL into a base URL without trailing slash."""
    address = address.strip()
    if not address:
        raise ValueError("Empty address")
    if "://" not in address:
        address = f"http://{address}"
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid address: {address}")
    return f"{parts.scheme}://{parts.netloc}"


def validate_log_filename(filename: str) -> str:
    """Return the filename if it is safe to use, else raise."""
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or "\x00" in filename
    ):
        raise InvalidFilenameError(f"Refusing unsafe log filename: {filename!r}")
    return filename


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_log_listing(payload: Any) -> LogListing:
    """Parse the ``/obd_logs`` JSON payload."""
    if not isinstance(payload, dict):
        raise WiCanResponseError("Unexpected /obd_logs payload (not an object)")
    raw_dbs = payload.get("databases") or []
    if not isinstance(raw_dbs, list):
        raise WiCanResponseError("Unexpected /obd_logs payload (databases)")
    databases: list[LogDatabase] = []
    for item in raw_dbs:
        if not isinstance(item, dict) or not item.get("filename"):
            continue
        databases.append(
            LogDatabase(
                filename=str(item["filename"]),
                created=str(item.get("created") or ""),
                size=_to_int(item.get("size")),
                status=str(item.get("status") or ""),
            )
        )
    current = payload.get("current_db")
    return LogListing(
        current_db=str(current) if current else None, databases=databases
    )


class WiCanClient:
    """Async client for one WiCAN device."""

    def __init__(self, session: aiohttp.ClientSession, address: str) -> None:
        """Initialize with a shared aiohttp session and an IP/hostname/URL."""
        self._session = session
        self.base_url = normalize_base_url(address)

    async def _request(self, path: str, timeout: float) -> aiohttp.ClientResponse:
        try:
            resp = await self._session.get(
                f"{self.base_url}{path}",
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
        except TimeoutError as err:
            raise WiCanConnectionError(f"Timeout requesting {path}") from err
        except aiohttp.ClientError as err:
            raise WiCanConnectionError(f"Cannot reach device: {err}") from err
        if resp.status == 404:
            resp.release()
            raise WiCanNotSupportedError(f"{path} not available (HTTP 404)", 404)
        if resp.status >= 400:
            resp.release()
            raise WiCanResponseError(f"HTTP {resp.status} on {path}", resp.status)
        return resp

    async def _get_json(self, path: str, timeout: float) -> Any:
        resp = await self._request(path, timeout)
        try:
            text = await resp.text(errors="replace")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise WiCanConnectionError(f"Connection lost reading {path}") from err
        finally:
            resp.release()
        try:
            return json.loads(text)
        except ValueError as err:
            raise WiCanResponseError(f"Invalid JSON from {path}") from err

    async def async_get_status(
        self, timeout: float = DEFAULT_PROBE_TIMEOUT
    ) -> dict[str, Any]:
        """Return ``/check_status``. Raises WiCanConnectionError when asleep."""
        data = await self._get_json("/check_status", timeout)
        if not isinstance(data, dict):
            raise WiCanResponseError("Unexpected /check_status payload")
        return data

    async def async_list_logs(
        self, timeout: float = DEFAULT_REQUEST_TIMEOUT
    ) -> LogListing:
        """Return the list of log databases on the SD card."""
        return parse_log_listing(await self._get_json("/obd_logs", timeout))

    async def async_download_log(
        self,
        filename: str,
        timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> bytes:
        """Download one log database and return its raw bytes."""
        validate_log_filename(filename)
        path = f"/obd_logs/{quote(filename, safe='')}"
        resp = await self._request(path, timeout)
        try:
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise WiCanResponseError(f"{filename} exceeds {max_bytes} bytes")
                chunks.append(chunk)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise WiCanConnectionError(f"Download of {filename} interrupted") from err
        finally:
            resp.release()
        return b"".join(chunks)
