"""Exceptions raised by the Zabbix API client."""

from collections.abc import Mapping
from typing import Any

# Messages Zabbix returns when the token is missing, unknown, disabled or expired.
# See ui/include/classes/api/services/CUser.php in the Zabbix source.
_AUTH_MESSAGES = (
    "Not authorized.",
    "Session terminated, re-login, please.",
    "API token expired.",
    "You must login to view this page.",
)
_PERMISSION_PREFIX = "No permissions"


class ZabbixError(Exception):
    """Base class for all Zabbix client errors."""


class ZabbixConnectionError(ZabbixError):
    """The Zabbix server could not be reached."""


class ZabbixSSLError(ZabbixConnectionError):
    """The TLS connection to the Zabbix server failed."""


class ZabbixInvalidResponseError(ZabbixError):
    """The server did not answer like a Zabbix JSON-RPC endpoint."""


class ZabbixApiError(ZabbixError):
    """Zabbix returned a JSON-RPC error."""

    def __init__(self, method: str, code: int, message: str, data: str) -> None:
        """Initialize the error."""
        super().__init__(f"{method}: {message} {data}".strip())
        self.method = method
        self.code = code
        self.message = message
        self.data = data


class ZabbixAuthError(ZabbixApiError):
    """The API token is invalid, disabled or expired."""


class ZabbixPermissionError(ZabbixApiError):
    """The token is valid but its user lacks permission for the request."""


def classify_error(method: str, error: Mapping[str, Any]) -> ZabbixApiError:
    """Turn a JSON-RPC error object into the most specific exception."""
    code = int(error.get("code", 0))
    message = str(error.get("message", ""))
    data = str(error.get("data", ""))
    if data in _AUTH_MESSAGES:
        return ZabbixAuthError(method, code, message, data)
    if data.startswith(_PERMISSION_PREFIX):
        return ZabbixPermissionError(method, code, message, data)
    return ZabbixApiError(method, code, message, data)
