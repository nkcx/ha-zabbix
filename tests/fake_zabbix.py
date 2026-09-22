"""A small fake Zabbix JSON-RPC server for tests.

It implements the subset of the Zabbix 7.x API the integration uses, with the
same authentication rules as the real server, over real HTTP on 127.0.0.1. The
default dataset imitates stock Zabbix templates; all names and addresses are
synthetic (RFC 5737 addresses).
"""

from collections.abc import Callable
import copy
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

TOKEN = "a" * 64
HIGH_CPU_EVENT = "900"

JsonObject = dict[str, Any]


def _interface(
    interfaceid: str,
    type_: int,
    ip: str,
    available: int,
    *,
    port: str = "10050",
    main: int = 1,
    error: str = "",
) -> JsonObject:
    return {
        "interfaceid": interfaceid,
        "type": str(type_),
        "main": str(main),
        "useip": "1",
        "ip": ip,
        "dns": "",
        "port": port,
        "available": str(available),
        "error": error,
    }


def _host(
    hostid: str,
    host: str,
    name: str,
    groupids: list[str],
    interfaces: list[JsonObject],
    *,
    active_available: int = 0,
    maintenance: bool = False,
) -> JsonObject:
    return {
        "hostid": hostid,
        "host": host,
        "name": name,
        "description": "",
        "status": "0",
        "maintenance_status": "1" if maintenance else "0",
        "maintenanceid": "0",
        "active_available": str(active_available),
        "interfaces": interfaces,
        "groupids": groupids,
    }


def _item(
    itemid: str,
    hostid: str,
    key: str,
    name: str,
    *,
    type_: int = 0,
    value_type: int = 0,
    units: str = "",
    value: str = "",
    clock: int = 1_784_000_000,
    tags: tuple[str, ...] = (),
    valuemapid: str = "0",
    interfaceid: str = "0",
    master_itemid: str = "0",
    state: int = 0,
    error: str = "",
    status: int = 0,
) -> JsonObject:
    return {
        "itemid": itemid,
        "hostid": hostid,
        "key_": key,
        "name": name,
        "name_resolved": name,
        "type": str(type_),
        "value_type": str(value_type),
        "units": units,
        "lastvalue": value,
        "lastclock": str(clock if value != "" else 0),
        "state": str(state),
        "error": error,
        "status": str(status),
        "valuemapid": valuemapid,
        "interfaceid": interfaceid,
        "master_itemid": master_itemid,
        "tags": [{"tag": tag, "value": ""} for tag in tags],
    }


def _problem(
    eventid: str,
    objectid: str,
    name: str,
    severity: int,
    *,
    acknowledged: bool = False,
    suppressed: bool = False,
    cause_eventid: str = "0",
    clock: int = 1_784_000_100,
) -> JsonObject:
    return {
        "eventid": eventid,
        "objectid": objectid,
        "name": name,
        "severity": str(severity),
        "acknowledged": "1" if acknowledged else "0",
        "suppressed": "1" if suppressed else "0",
        "clock": str(clock),
        "opdata": "",
        "cause_eventid": cause_eventid,
        "tags": [{"tag": "scope", "value": "availability"}],
    }


HA = ("homeassistant",)


def default_dataset() -> JsonObject:
    """Return a dataset shaped like a small stock Zabbix installation."""
    groups = [
        {"groupid": "2", "name": "Linux servers"},
        {"groupid": "4", "name": "Zabbix servers"},
        {"groupid": "7", "name": "Network devices"},
        {"groupid": "9", "name": "Other"},
    ]
    hosts = [
        _host(
            "10084",
            "Zabbix server",
            "Zabbix server",
            ["4"],
            [_interface("1", 1, "127.0.0.1", 1)],
        ),
        _host(
            "10500",
            "web-01",
            "Web server 01",
            ["2"],
            [_interface("11", 1, "192.0.2.10", 1)],
            active_available=1,
        ),
        _host(
            "10501",
            "db-01",
            "Database 01",
            ["2"],
            [
                _interface(
                    "12",
                    1,
                    "192.0.2.11",
                    2,
                    error="Get value from agent failed: cannot connect",
                ),
                _interface("13", 2, "192.0.2.12", 1, port="161"),
            ],
        ),
        _host(
            "10600",
            "switch-01",
            "Switch 01",
            ["7"],
            [_interface("14", 2, "192.0.2.20", 0, port="161")],
        ),
        _host("10700", "api-check", "API check", ["2"], []),
        _host(
            "10999",
            "other-01",
            "Other 01",
            ["9"],
            [_interface("15", 1, "192.0.2.99", 1)],
        ),
    ]
    valuemaps = {
        "10": {
            "valuemapid": "10",
            "name": "Zabbix agent ping status",
            "mappings": [{"type": "0", "value": "1", "newvalue": "Up"}],
        },
        "11": {
            "valuemapid": "11",
            "name": "Service state",
            "mappings": [
                {"type": "0", "value": "0", "newvalue": "Down"},
                {"type": "0", "value": "1", "newvalue": "Up"},
            ],
        },
    }
    items = [
        # Zabbix server self-monitoring (template "Zabbix server health").
        _item(
            "23000",
            "10084",
            "zabbix[wcache,values]",
            "Zabbix server: Number of processed values per second",
            type_=5,
            value="12.5",
        ),
        _item(
            "23001",
            "10084",
            "zabbix[queue]",
            "Zabbix server: Queue",
            type_=5,
            value_type=3,
            value="0",
        ),
        _item(
            "23002",
            "10084",
            "zabbix[process,poller,avg,busy]",
            "Zabbix server: Utilization of poller data collector processes, in %",
            type_=5,
            units="%",
            value="1.5",
        ),
        _item(
            "23003",
            "10084",
            "zabbix[version]",
            "Zabbix server: Version",
            type_=5,
            value_type=1,
            value="7.4.14",
        ),
        _item(
            "23004",
            "10084",
            "zabbix[host,agent,available]",
            "Linux: Zabbix agent availability",
            type_=5,
            value_type=3,
            value="1",
        ),
        _item(
            "23005",
            "10084",
            "system.cpu.util",
            "Linux: CPU utilization",
            units="%",
            value="5",
            interfaceid="1",
        ),
        _item(
            "23006",
            "10084",
            "zabbix[rcache,buffer,pused]",
            "Zabbix server: Configuration cache, % used",
            type_=5,
            units="%",
            value="20",
        ),
        _item(
            "23008",
            "10084",
            "zabbix[stats,127.0.0.1,10051]",
            "Zabbix server: Zabbix stats",
            type_=5,
            value_type=4,
            value="{}",
        ),
        _item(
            "23009",
            "10084",
            "zabbix.stats.version",
            "Zabbix server: Stats version",
            type_=18,
            value_type=1,
            value="7.4.14",
            master_itemid="23008",
        ),
        # web-01: Linux by Zabbix agent, a few tagged items.
        _item(
            "24000",
            "10500",
            "system.cpu.util",
            "Linux: CPU utilization",
            units="%",
            value="3.2",
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24001",
            "10500",
            "vm.memory.utilization",
            "Linux: Memory utilization",
            units="%",
            value="41",
            interfaceid="11",
        ),
        _item(
            "24002",
            "10500",
            "system.uptime",
            "Linux: System uptime",
            value_type=3,
            units="uptime",
            value="86400",
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24003",
            "10500",
            "agent.ping",
            "Linux: Zabbix agent ping",
            value_type=3,
            value="1",
            valuemapid="10",
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24005",
            "10500",
            "vfs.file.contents[/etc/motd]",
            "Message of the day",
            value_type=4,
            value="x" * 300,
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24006",
            "10500",
            "web.test.in[Homepage,,bps]",
            'Download speed for scenario "Homepage".',
            type_=9,
            units="Bps",
            value="1200.5",
            tags=HA,
        ),
        _item(
            "24007",
            "10500",
            'net.if.in["eth0"]',
            "Interface eth0: Bits received",
            units="bps",
            value="1000",
            interfaceid="11",
        ),
        _item(
            "24008",
            "10500",
            "system.localtime",
            "Linux: System local time",
            value_type=3,
            units="unixtime",
            value="1784000000",
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24009",
            "10500",
            "vfs.fs.dependent.size[/,pused]",
            "FS [/]: Space: Used, in %",
            type_=18,
            units="%",
            state=1,
            error="Cannot obtain filesystem information",
            tags=HA,
        ),
        _item(
            "24010",
            "10500",
            "system.hostname",
            "Linux: System name",
            type_=7,
            value_type=1,
            value="web-01",
        ),
        _item(
            "24011",
            "10500",
            "icmpping",
            "ICMP: ICMP ping",
            type_=3,
            value_type=3,
            value="3",
            valuemapid="11",
            tags=HA,
        ),
        _item(
            "24012",
            "10500",
            "screenshot",
            "Screenshot",
            value_type=5,
            tags=HA,
        ),
        _item(
            "24013",
            "10500",
            "sensor.temp.value[cpu]",
            "CPU temperature",
            units="°C",
            value="45",
            tags=HA,
            interfaceid="11",
        ),
        _item(
            "24014",
            "10500",
            "custom.metric",
            "Custom metric",
            units="{#UNITS}",
            value="7",
            tags=HA,
        ),
        _item(
            "24015",
            "10500",
            "zabbix[host,agent,available]",
            "Linux: Zabbix agent availability",
            type_=5,
            value_type=3,
            value="1",
        ),
        _item(
            "24016",
            "10500",
            "disabled.item",
            "Disabled item",
            value="1",
            tags=HA,
            status=1,
            interfaceid="11",
        ),
        # db-01
        _item(
            "25000",
            "10501",
            "system.cpu.util",
            "Linux: CPU utilization",
            units="%",
            tags=HA,
            interfaceid="12",
        ),
        _item(
            "25001",
            "10501",
            "ifOperStatus[1]",
            "Interface 1: Operational status",
            type_=20,
            value_type=3,
            value="1",
            interfaceid="13",
        ),
        # other-01 (group not selected by default)
        _item(
            "26000",
            "10999",
            "system.cpu.util",
            "Linux: CPU utilization",
            units="%",
            value="9",
            tags=HA,
            interfaceid="15",
        ),
    ]
    problems = [
        _problem(HIGH_CPU_EVENT, "13000", "High CPU utilization", 4),
        _problem("901", "13001", "Zabbix agent is not available", 3, acknowledged=True),
        _problem("902", "13002", "Disk is full", 5, suppressed=True),
        _problem("903", "13003", "Service is down", 2, cause_eventid="901"),
        _problem("904", "13004", "Other host problem", 1),
    ]
    triggers = {
        "13000": ["10500"],
        "13001": ["10501"],
        "13002": ["10501"],
        "13003": ["10501"],
        "13004": ["10999"],
        "13005": ["10500"],
    }
    return {
        "groups": groups,
        "hosts": hosts,
        "items": items,
        "valuemaps": valuemaps,
        "problems": problems,
        "triggers": triggers,
    }


class ApiFailure(Exception):
    """Raised by handlers to return a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: str) -> None:
        """Initialize."""
        super().__init__(data)
        self.error = {"code": code, "message": message, "data": data}


NOT_AUTHORIZED = ApiFailure(-32602, "Invalid params.", "Not authorized.")


def _matches_filter(obj: JsonObject, filters: JsonObject) -> bool:
    for key, wanted in filters.items():
        values = wanted if isinstance(wanted, list) else [wanted]
        if str(obj.get(key)) not in {str(value) for value in values}:
            return False
    return True


def _count(rows: list[JsonObject], params: JsonObject, field_name: str) -> Any:
    if params.get("groupCount"):
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[field_name]] = counts.get(row[field_name], 0) + 1
        return [
            {"rowscount": str(count), field_name: key} for key, count in counts.items()
        ]
    return str(len(rows))


@dataclass
class FakeZabbix:
    """State and behaviour of the fake server."""

    version: str = "7.4.14"
    token: str = TOKEN
    user: JsonObject = field(
        default_factory=lambda: {"userid": "5", "username": "ha", "type": 1}
    )
    data: JsonObject = field(default_factory=default_dataset)
    calls: list[tuple[str, Any, str | None]] = field(default_factory=list)
    failures: dict[str, ApiFailure] = field(default_factory=dict)
    http_status: int | None = None
    raw_body: str | None = None
    url: str = ""
    next_id: int = 100
    handlers: dict[str, Callable[[Any], Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Register method handlers."""
        self.handlers = {
            "hostgroup.get": self._hostgroup_get,
            "host.get": self._host_get,
            "item.get": self._item_get,
            "trigger.get": self._trigger_get,
            "problem.get": self._problem_get,
            "event.acknowledge": self._event_acknowledge,
            "maintenance.get": self._maintenance_get,
            "maintenance.create": self._maintenance_create,
            "maintenance.update": self._maintenance_update,
            "maintenance.delete": self._maintenance_delete,
        }
        self.data.setdefault("maintenances", [])

    def method_calls(self, method: str) -> list[Any]:
        """Return the params of every call to ``method``."""
        return [params for name, params, _ in self.calls if name == method]

    def item(self, itemid: str) -> JsonObject:
        """Return a raw item."""
        return next(item for item in self.data["items"] if item["itemid"] == itemid)

    def host(self, hostid: str) -> JsonObject:
        """Return a raw host."""
        return next(host for host in self.data["hosts"] if host["hostid"] == hostid)

    # HTTP -----------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.Response:
        """Handle a JSON-RPC request."""
        if self.http_status is not None:
            return web.Response(status=self.http_status, text="error")
        if self.raw_body is not None:
            return web.Response(text=self.raw_body, content_type="text/html")
        body = await request.json()
        method = body["method"]
        params = body.get("params", {})
        auth = request.headers.get("Authorization")
        self.calls.append((method, copy.deepcopy(params), auth))
        try:
            result = self._dispatch(method, params, auth)
        except ApiFailure as failure:
            return web.json_response(
                {"jsonrpc": "2.0", "error": failure.error, "id": body.get("id")}
            )
        return web.json_response({"jsonrpc": "2.0", "result": result, "id": body["id"]})

    def _dispatch(self, method: str, params: Any, auth: str | None) -> Any:
        if method in self.failures:
            raise self.failures[method]
        if method in ("apiinfo.version", "user.checkAuthentication"):
            if auth is not None:
                raise ApiFailure(
                    -32602,
                    "Invalid params.",
                    f'The "{method}" method must be called without authorization header.',
                )
            if method == "apiinfo.version":
                return self.version
            if params.get("token") != self.token:
                raise NOT_AUTHORIZED
            return dict(self.user)
        if auth != f"Bearer {self.token}":
            raise NOT_AUTHORIZED
        if method not in self.handlers:
            raise ApiFailure(
                -32601, "Method not found.", f'Incorrect method "{method}".'
            )
        return self.handlers[method](params)

    # Handlers -------------------------------------------------------------------

    def _host_groups_of(self, host: JsonObject) -> list[str]:
        return list(host["groupids"])

    def _monitored_host_ids(self) -> set[str]:
        return {host["hostid"] for host in self.data["hosts"] if host["status"] == "0"}

    def _hostgroup_get(self, params: JsonObject) -> Any:
        groups = self.data["groups"]
        if "groupids" in params:
            groups = [g for g in groups if g["groupid"] in params["groupids"]]
        result = []
        for group in groups:
            hosts = [
                host
                for host in self.data["hosts"]
                if group["groupid"] in self._host_groups_of(host)
            ]
            if params.get("with_monitored_hosts") and not any(
                host["status"] == "0" for host in hosts
            ):
                continue
            result.append({**group, "hosts": str(len(hosts))})
        return result

    def _host_get(self, params: JsonObject) -> Any:
        hosts = self.data["hosts"]
        if "groupids" in params:
            hosts = [
                host
                for host in hosts
                if set(self._host_groups_of(host)) & set(params["groupids"])
            ]
        if "hostids" in params:
            hosts = [host for host in hosts if host["hostid"] in params["hostids"]]
        if params.get("monitored_hosts"):
            hosts = [host for host in hosts if host["status"] == "0"]
        if "filter" in params:
            hosts = [host for host in hosts if _matches_filter(host, params["filter"])]
        if params.get("countOutput"):
            return str(len(hosts))
        result = []
        for host in hosts:
            row = {key: value for key, value in host.items() if key != "groupids"}
            if "selectHostGroups" in params:
                row["hostgroups"] = [{"groupid": gid} for gid in host["groupids"]]
            if "selectInterfaces" not in params:
                row.pop("interfaces")
            result.append(row)
        return result

    def _item_get(self, params: JsonObject) -> Any:
        items = list(self.data["items"])
        if not params.get("webitems"):
            items = [item for item in items if item["type"] != "9"]
        if "itemids" in params:
            items = [item for item in items if item["itemid"] in params["itemids"]]
        if "hostids" in params:
            items = [item for item in items if item["hostid"] in params["hostids"]]
        if "interfaceids" in params:
            items = [
                item for item in items if item["interfaceid"] in params["interfaceids"]
            ]
        if params.get("monitored"):
            monitored = self._monitored_host_ids()
            items = [
                item
                for item in items
                if item["status"] == "0" and item["hostid"] in monitored
            ]
        if "filter" in params:
            items = [item for item in items if _matches_filter(item, params["filter"])]
        if "search" in params:
            prefix = params["search"]["key_"]
            assert params.get("startSearch")
            items = [item for item in items if item["key_"].startswith(prefix)]
        for tag_filter in params.get("tags", []):
            assert tag_filter["operator"] == 4
            items = [
                item
                for item in items
                if any(tag["tag"] == tag_filter["tag"] for tag in item["tags"])
            ]
        if params.get("countOutput"):
            group_field = "interfaceid" if "interfaceids" in params else "hostid"
            return _count(items, params, group_field)
        result = []
        for item in items:
            row = dict(item)
            if "selectValueMap" in params:
                row["valuemap"] = self.data["valuemaps"].get(item["valuemapid"], [])
            if "selectTags" not in params:
                row.pop("tags")
            result.append(row)
        return result

    def _trigger_get(self, params: JsonObject) -> Any:
        triggers = self.data["triggers"]
        if params.get("countOutput"):
            return str(len(triggers))
        return [
            {
                "triggerid": triggerid,
                "hosts": [{"hostid": hostid} for hostid in host_ids],
            }
            for triggerid, host_ids in triggers.items()
            if triggerid in params["triggerids"]
        ]

    def _problem_get(self, params: JsonObject) -> Any:
        return copy.deepcopy(self.data["problems"])

    def _event_acknowledge(self, params: JsonObject) -> Any:
        action = params["action"]
        for problem in self.data["problems"]:
            if problem["eventid"] not in params["eventids"]:
                continue
            if action & 2:
                problem["acknowledged"] = "1"
            if action & 16:
                problem["acknowledged"] = "0"
        if action & 1:
            self.data["problems"] = [
                problem
                for problem in self.data["problems"]
                if problem["eventid"] not in params["eventids"]
            ]
        return {"eventids": params["eventids"]}

    def _maintenance_get(self, params: JsonObject) -> Any:
        prefix = params["search"]["name"]
        return [
            {
                "maintenanceid": maintenance["maintenanceid"],
                "name": maintenance["name"],
                "hosts": [{"hostid": hostid} for hostid in maintenance["hostids"]],
            }
            for maintenance in self.data["maintenances"]
            if maintenance["name"].startswith(prefix)
        ]

    def _maintenance_create(self, params: JsonObject) -> Any:
        self.next_id += 1
        maintenance_id = str(self.next_id)
        self.data["maintenances"].append(
            {
                "maintenanceid": maintenance_id,
                "name": params["name"],
                "hostids": [host["hostid"] for host in params["hosts"]],
                "params": params,
            }
        )
        return {"maintenanceids": [maintenance_id]}

    def _maintenance_update(self, params: JsonObject) -> Any:
        for maintenance in self.data["maintenances"]:
            if maintenance["maintenanceid"] == params["maintenanceid"]:
                maintenance["hostids"] = [host["hostid"] for host in params["hosts"]]
        return {"maintenanceids": [params["maintenanceid"]]}

    def _maintenance_delete(self, params: list[str]) -> Any:
        self.data["maintenances"] = [
            maintenance
            for maintenance in self.data["maintenances"]
            if maintenance["maintenanceid"] not in params
        ]
        return {"maintenanceids": params}


def make_app(fake: FakeZabbix) -> web.Application:
    """Return an aiohttp app serving the fake API at any */api_jsonrpc.php."""
    app = web.Application()
    app.router.add_post("/{prefix:.*}api_jsonrpc.php", fake.handle)
    return app
