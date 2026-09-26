"""Tests for the standalone Zabbix API client."""

from collections.abc import AsyncGenerator
import ssl
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.zabbix.api import (
    AcknowledgeAction,
    Availability,
    HostInterface,
    InterfaceType,
    Item,
    Problem,
    UserType,
    ValueMap,
    ValueType,
    ZabbixApiError,
    ZabbixAuthError,
    ZabbixClient,
    ZabbixConnectionError,
    ZabbixInvalidResponseError,
    ZabbixPermissionError,
    ZabbixSSLError,
    frontend_url,
    normalize_url,
    parse_version,
)
from custom_components.zabbix.api.errors import classify_error

from .fake_zabbix import TOKEN, ApiFailure, FakeZabbix


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    """Return a plain aiohttp session."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def client(fake_zabbix: FakeZabbix, session: aiohttp.ClientSession) -> ZabbixClient:
    """Return a client for the fake server."""
    return ZabbixClient(fake_zabbix.url, TOKEN, session)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("zabbix.example.com", "https://zabbix.example.com/api_jsonrpc.php"),
        ("https://Zabbix.Example.com/", "https://zabbix.example.com/api_jsonrpc.php"),
        (
            "https://zabbix.example.com/zabbix/",
            "https://zabbix.example.com/zabbix/api_jsonrpc.php",
        ),
        (
            "https://zabbix.example.com/zabbix/zabbix.php?action=dashboard.view#x",
            "https://zabbix.example.com/zabbix/api_jsonrpc.php",
        ),
        (
            "http://zabbix.example.com:8080/api_jsonrpc.php",
            "http://zabbix.example.com:8080/api_jsonrpc.php",
        ),
        (
            "  https://user@zabbix.example.com/monitoring  ",
            "https://zabbix.example.com/monitoring/api_jsonrpc.php",
        ),
    ],
)
def test_normalize_url(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


@pytest.mark.parametrize("url", ["ftp://zabbix.example.com", "https://", "http:///x"])
def test_normalize_url_invalid(url: str) -> None:
    with pytest.raises(ValueError, match="Invalid URL"):
        normalize_url(url)


def test_frontend_url() -> None:
    assert (
        frontend_url("https://zabbix.example.com/zabbix/api_jsonrpc.php")
        == "https://zabbix.example.com/zabbix/"
    )


async def test_version_and_token_user_are_unauthenticated(
    client: ZabbixClient, fake_zabbix: FakeZabbix
) -> None:
    assert await client.async_get_version() == "7.4.14"
    user = await client.async_get_token_user()
    assert user.username == "ha"
    assert user.type is UserType.USER
    assert all(auth is None for _, _, auth in fake_zabbix.calls)
    assert client.frontend_url == fake_zabbix.url


async def test_authenticated_calls_send_bearer_token(
    client: ZabbixClient, fake_zabbix: FakeZabbix
) -> None:
    groups = await client.async_get_host_groups()
    assert [group.name for group in groups] == [
        "Linux servers",
        "Zabbix servers",
        "Network devices",
        "Other",
    ]
    assert groups[0].host_count == 3
    assert fake_zabbix.calls[-1][2] == f"Bearer {TOKEN}"
    assert fake_zabbix.calls[-1][1]["with_monitored_hosts"] is True
    selected = await client.async_get_host_groups(["7"])
    assert [group.group_id for group in selected] == ["7"]


async def test_bad_token(
    fake_zabbix: FakeZabbix, session: aiohttp.ClientSession
) -> None:
    client = ZabbixClient(fake_zabbix.url, "b" * 64, session)
    with pytest.raises(ZabbixAuthError) as err:
        await client.async_get_token_user()
    assert err.value.data == "Not authorized."
    with pytest.raises(ZabbixAuthError):
        await client.async_get_hosts(["2"])


async def test_hosts(client: ZabbixClient) -> None:
    hosts = {host.host_id: host for host in await client.async_get_hosts(["2", "7"])}
    assert set(hosts) == {"10500", "10501", "10600", "10700"}
    db = hosts["10501"]
    assert db.name == "Database 01"
    assert db.host == "db-01"
    assert db.group_ids == frozenset({"2"})
    assert [interface.type for interface in db.interfaces] == [
        InterfaceType.AGENT,
        InterfaceType.SNMP,
    ]
    assert db.interfaces[0].available is Availability.UNAVAILABLE
    assert db.interfaces[0].address == "192.0.2.11:10050"
    assert hosts["10500"].active_available is Availability.AVAILABLE
    assert await client.async_get_hosts([]) == []


async def test_host_names(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    assert await client.async_get_host_names(["10084", "10500"]) == {
        "10084": "Zabbix server",
        "10500": "Web server 01",
    }
    calls = len(fake_zabbix.calls)
    assert await client.async_get_host_names([]) == {}
    assert len(fake_zabbix.calls) == calls


async def test_items(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    items = await client.async_get_items(host_ids=["10500"])
    by_key = {item.key: item for item in items}
    assert "disabled.item" not in by_key
    assert "web.test.in[Homepage,,bps]" in by_key
    ping = by_key["agent.ping"]
    assert ping.value_type is ValueType.UNSIGNED
    assert ping.value_map is not None
    assert ping.value_map.options == ["Up"]
    assert ping.has_tag("homeassistant")
    assert by_key["vm.memory.utilization"].value_map is None
    tagged = await client.async_get_items(host_ids=["10500"], tag="homeassistant")
    assert "vm.memory.utilization" not in {item.key for item in tagged}
    assert fake_zabbix.method_calls("item.get")[-1]["tags"] == [
        {"tag": "homeassistant", "operator": 4}
    ]
    everything = await client.async_get_items()
    assert {item.host_id for item in everything} >= {"10084", "10999"}
    calls = len(fake_zabbix.calls)
    assert await client.async_get_items(host_ids=[]) == []
    assert len(fake_zabbix.calls) == calls


async def test_server_items(client: ZabbixClient) -> None:
    items = {item.key: item for item in await client.async_get_server_items()}
    assert "zabbix[host,agent,available]" not in items
    assert "system.cpu.util" not in items
    # Dependent items of server items are included.
    assert items["zabbix.stats.version"].master_item_id == "23008"
    assert {item.host_id for item in items.values()} == {"10084"}


async def test_values(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    values = await client.async_get_item_values(["24000", "24006", "24009", "25000"])
    assert values["24000"].last_value == "3.2"
    assert values["24000"].has_value
    assert values["24006"].last_value == "1200.5"
    assert not values["24009"].supported
    assert values["24009"].error == "Cannot obtain filesystem information"
    assert not values["25000"].has_value
    calls = len(fake_zabbix.calls)
    assert await client.async_get_item_values([]) == {}
    assert len(fake_zabbix.calls) == calls


async def test_counts(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    assert await client.async_get_interface_item_counts(["11", "12", "13", "14"]) == {
        "11": 8,
        "12": 1,
        "13": 1,
    }
    assert await client.async_get_active_item_counts(["10500", "10501"]) == {"10500": 1}
    counts = await client.async_get_item_counts(["10500", "10501"])
    assert counts == {"10500": 15, "10501": 2}
    server = await client.async_get_server_counts()
    assert server.hosts == 6
    assert server.items_unsupported == 1
    assert server.triggers == 6
    calls = len(fake_zabbix.calls)
    assert await client.async_get_interface_item_counts([]) == {}
    assert await client.async_get_active_item_counts([]) == {}
    assert await client.async_get_item_counts([]) == {}
    assert len(fake_zabbix.calls) == calls


async def test_problems(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    problems = {
        problem.event_id: problem for problem in await client.async_get_problems()
    }
    assert problems["900"].name == "High CPU utilization"
    assert problems["901"].acknowledged
    assert problems["902"].suppressed
    assert problems["903"].is_symptom
    assert not problems["900"].is_symptom
    assert problems["900"].tags[0].tag == "scope"
    hosts = await client.async_get_trigger_hosts(["13000", "13003"])
    assert hosts == {"13000": ("10500",), "13003": ("10501",)}
    fake_zabbix.data["hidden_triggers"] = ["13003"]
    assert await client.async_get_visible_trigger_ids(["13000", "13003"]) == {"13000"}
    calls = len(fake_zabbix.calls)
    assert await client.async_get_visible_trigger_ids([]) == set()
    assert await client.async_get_trigger_hosts([]) == {}
    assert len(fake_zabbix.calls) == calls


async def test_acknowledge(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    await client.async_acknowledge(
        ["900"], AcknowledgeAction.ACKNOWLEDGE, message="On it", severity=5
    )
    params = fake_zabbix.method_calls("event.acknowledge")[-1]
    assert params == {
        "eventids": ["900"],
        "message": "On it",
        "severity": 5,
        "action": 2 | 4 | 8,
    }
    await client.async_acknowledge(["900"], AcknowledgeAction.CLOSE)
    assert fake_zabbix.method_calls("event.acknowledge")[-1] == {
        "eventids": ["900"],
        "action": 1,
    }


async def test_maintenance(client: ZabbixClient, fake_zabbix: FakeZabbix) -> None:
    maintenance_id = await client.async_create_maintenance(
        "Home Assistant: web", ["10500", "10501"], start=1000, duration=3600
    )
    params = fake_zabbix.method_calls("maintenance.create")[-1]
    assert params["active_till"] == 4600
    assert params["maintenance_type"] == 0
    assert params["timeperiods"] == [
        {"timeperiod_type": 0, "start_date": 1000, "period": 3600}
    ]
    await client.async_create_maintenance(
        "Other", ["10500"], start=1000, duration=60, collect_data=False
    )
    assert fake_zabbix.method_calls("maintenance.create")[-1]["maintenance_type"] == 1
    found = await client.async_get_maintenances("Home Assistant: ")
    assert [(m.maintenance_id, m.host_ids) for m in found] == [
        (maintenance_id, ("10500", "10501"))
    ]
    await client.async_set_maintenance_hosts(maintenance_id, ["10501"])
    found = await client.async_get_maintenances("Home Assistant: ")
    assert found[0].host_ids == ("10501",)
    await client.async_delete_maintenances([maintenance_id])
    assert await client.async_get_maintenances("Home Assistant: ") == []
    calls = len(fake_zabbix.calls)
    await client.async_delete_maintenances([])
    assert len(fake_zabbix.calls) == calls


@pytest.mark.parametrize(
    ("failure", "exception"),
    [
        (ApiFailure(-32602, "Invalid params.", "Not authorized."), ZabbixAuthError),
        (
            ApiFailure(-32500, "Application error.", "API token expired."),
            ZabbixAuthError,
        ),
        (
            ApiFailure(
                -32500,
                "Application error.",
                "No permissions to referred object or it does not exist!",
            ),
            ZabbixPermissionError,
        ),
        (ApiFailure(-32602, "Invalid params.", "Invalid parameter."), ZabbixApiError),
    ],
)
async def test_api_errors(
    client: ZabbixClient,
    fake_zabbix: FakeZabbix,
    failure: ApiFailure,
    exception: type[Exception],
) -> None:
    fake_zabbix.failures["hostgroup.get"] = failure
    with pytest.raises(exception) as err:
        await client.async_get_host_groups()
    assert type(err.value) is exception


async def test_unknown_method(client: ZabbixClient) -> None:
    with pytest.raises(ZabbixApiError) as err:
        await client.call("foo.get")
    assert err.value.code == -32601
    assert "foo.get" in str(err.value)


def test_classify_error_without_fields() -> None:
    error = classify_error("x.get", {})
    assert type(error) is ZabbixApiError
    assert error.code == 0


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (500, ZabbixConnectionError),
        (502, ZabbixConnectionError),
        (404, ZabbixInvalidResponseError),
    ],
)
async def test_http_errors(
    client: ZabbixClient,
    fake_zabbix: FakeZabbix,
    status: int,
    exception: type[Exception],
) -> None:
    fake_zabbix.http_status = status
    with pytest.raises(exception):
        await client.async_get_version()


@pytest.mark.parametrize(
    "body",
    [
        "<html>not zabbix</html>",
        '["jsonrpc"]',
        '{"jsonrpc": "1.0", "result": 1}',
        '{"jsonrpc": "2.0", "id": 1}',
    ],
)
async def test_invalid_responses(
    client: ZabbixClient, fake_zabbix: FakeZabbix, body: str
) -> None:
    fake_zabbix.raw_body = body
    with pytest.raises(ZabbixInvalidResponseError):
        await client.async_get_version()


async def test_unexpected_result_types(
    client: ZabbixClient, fake_zabbix: FakeZabbix
) -> None:
    fake_zabbix.raw_body = '{"jsonrpc": "2.0", "result": [], "id": 1}'
    with pytest.raises(ZabbixInvalidResponseError):
        await client.async_get_version()
    with pytest.raises(ZabbixInvalidResponseError):
        await client.async_get_token_user()


async def test_error_object_not_a_dict(
    client: ZabbixClient, fake_zabbix: FakeZabbix
) -> None:
    fake_zabbix.raw_body = '{"jsonrpc": "2.0", "error": "boom", "id": 1}'
    with pytest.raises(ZabbixApiError):
        await client.async_get_version()


async def test_connection_refused(
    session: aiohttp.ClientSession, socket_enabled: None
) -> None:
    client = ZabbixClient("http://127.0.0.1:1/", TOKEN, session)
    with pytest.raises(ZabbixConnectionError):
        await client.async_get_version()


async def test_timeout(client: ZabbixClient) -> None:
    with (
        patch.object(aiohttp.ClientSession, "post", side_effect=TimeoutError),
        pytest.raises(ZabbixConnectionError, match="timeout"),
    ):
        await client.async_get_version()


async def test_ssl_error(client: ZabbixClient) -> None:
    with (
        patch.object(aiohttp.ClientSession, "post", side_effect=ssl.SSLError("bad")),
        pytest.raises(ZabbixSSLError),
    ):
        await client.async_get_version()


def test_parse_version() -> None:
    assert parse_version("7.4.12") == (7, 4, 12)
    assert parse_version("8.0.0alpha1") == (8, 0, 0)
    assert parse_version("7.0") == (7, 0, 0)
    assert parse_version("x") == (0, 0, 0)


def _value_map(*mappings: tuple[int, str, str]) -> ValueMap:
    value_map = ValueMap.from_api(
        {
            "valuemapid": "1",
            "name": "Test",
            "mappings": [
                {"type": str(type_), "value": value, "newvalue": new}
                for type_, value, new in mappings
            ],
        }
    )
    assert value_map is not None
    return value_map


def test_value_map_types() -> None:
    value_map = _value_map(
        (0, "1", "One"),
        (3, "10-20,30", "Range"),
        (1, "100", "Big"),
        (2, "-5", "Small"),
        (4, "^err", "Error"),
        (4, "(", "Broken regex"),
        (5, "", "Other"),
    )
    assert value_map.map("1") == "One"
    assert value_map.map("1.0") == "One"
    assert value_map.map("15") == "Range"
    assert value_map.map("30") == "Range"
    assert value_map.map("150") == "Big"
    assert value_map.map("-10") == "Small"
    assert value_map.map("error: x") == "Error"
    assert value_map.map("25") == "Other"
    assert value_map.map("text") == "Other"
    assert value_map.options == [
        "One",
        "Range",
        "Big",
        "Small",
        "Error",
        "Broken regex",
        "Other",
    ]


def test_value_map_without_default() -> None:
    value_map = _value_map((0, "0", "Down"), (3, "bad range", "Nope"))
    assert value_map.map("1") is None
    assert value_map.map("x") is None


def test_value_map_numeric_types_ignore_text() -> None:
    value_map = _value_map((1, "abc", "Never"), (2, "5", "Low"))
    assert value_map.map("3") == "Low"
    assert value_map.map("abc") is None


def test_value_map_from_api_empty() -> None:
    assert ValueMap.from_api({}) is None
    assert ValueMap.from_api({"valuemapid": ""}) is None


def test_item_from_api_is_tolerant() -> None:
    item = Item.from_api(
        {
            "itemid": "1",
            "name": "Fallback name",
            "value_type": "99",
            "type": "x",
            "valuemap": [],
        }
    )
    assert item.name == "Fallback name"
    assert item.value_type is ValueType.TEXT
    assert item.type == 0
    assert item.value_map is None


def test_interface_address_formats() -> None:
    base = {
        "interfaceid": "1",
        "type": "1",
        "main": "1",
        "port": "10050",
        "available": "9",
    }
    assert HostInterface.from_api(
        {**base, "useip": "1", "ip": "2001:db8::1"}
    ).address == ("[2001:db8::1]:10050")
    dns = HostInterface.from_api({**base, "useip": "0", "dns": "host.example.com"})
    assert dns.address == "host.example.com:10050"
    assert dns.available is Availability.UNKNOWN


def test_problem_symptom_flag() -> None:
    assert not Problem.from_api({"eventid": "1", "cause_eventid": ""}).is_symptom
    assert Problem.from_api({"eventid": "1", "cause_eventid": "7"}).is_symptom
