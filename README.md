# Zabbix for Home Assistant

[![Validate](https://github.com/nkcx/ha-zabbix/actions/workflows/validate.yml/badge.svg)](https://github.com/nkcx/ha-zabbix/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

Brings your [Zabbix](https://www.zabbix.com/) monitoring into Home Assistant. Let
Zabbix do what it's good at (collecting data, triggers, dependencies, alerting), and
use Home Assistant's dashboards and automations on top of it.

- Every Zabbix host becomes a **device** with its availability, problems and
  maintenance status.
- The Zabbix server itself is a **"Zabbix" device**: problem count, version, host,
  item and trigger counts, values per second, queue, caches.
- The Zabbix **items you choose** (by tagging them in Zabbix) become sensors.
- **Problem events** for automations: new, resolved, acknowledged, severity changed…
- **Actions** to acknowledge and close problems and to put hosts into maintenance.

*Independent community project, not affiliated with Zabbix SIA; see
[License and trademarks](#license-and-trademarks).*

The integration reports what Zabbix reports: its problem count, its availability,
its item names and value maps. It doesn't filter or reinterpret your data.

> [!NOTE]
> This integration uses the `zabbix` domain and **replaces the built-in Zabbix
> integration**, which only pushes Home Assistant states to Zabbix via YAML. Pushing
> Home Assistant data to Zabbix is planned but not available yet. If you use the
> built-in integration's YAML configuration, don't install this one yet.

## Requirements

- Zabbix **7.0 or newer** (tested on 7.0 and 7.4).
- Home Assistant **2026.9** or newer.
- A Zabbix API token (see [Creating an API token](#creating-an-api-token)).

## Installation

1. In HACS, open the menu → **Custom repositories**, add
   `https://github.com/nkcx/ha-zabbix` with type **Integration**.
2. Install **Zabbix** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Zabbix**.

## Configuration

| Step | Setting | Description |
|---|---|---|
| Connect | URL | Address of the Zabbix web interface, e.g. `https://zabbix.example.com/zabbix/`. The API address (`…/api_jsonrpc.php`) also works. |
| | API token | See below. |
| | Verify SSL certificate | Turn off only for self-signed certificates. |
| Host groups | Host groups | Each monitored host in these groups becomes a device. |
| Items | Items to import | See [Choosing items](#choosing-items). |
| | Tag name | Items with a tag of this name become entities. Default `homeassistant`. |

Host groups, item mode, tag name and the **update interval** (default 30 s) can be
changed later under **Configure**. The URL can be changed with **Reconfigure**.

### Creating an API token

Use a dedicated Zabbix user with only the access it needs:

1. **Users → User roles → Create user role**, e.g. *Home Assistant*:
   - User type: **User** (or **Admin** if you want to use the maintenance actions).
   - API access: **enabled**.
   - Under *Access to actions*, allow **Acknowledge problems** and **Close
     problems** only if you want to use those actions.
2. **Users → User groups → Create user group**, e.g. *Home Assistant*, and give it
   **Read** permission on the host groups you want in Home Assistant (**Read-write**
   is needed for the maintenance actions).
3. **Users → Users → Create user** in that group with that role.
4. **Users → API tokens → Create API token** for that user. Copy the token; Zabbix
   shows it only once.

Problem counts and the Zabbix device's counters include everything this user can
see.

## Choosing items

A Zabbix item (check) becomes a Home Assistant sensor depending on the **item mode**:

| Mode | Item entities |
|---|---|
| **Zabbix server + tagged** (default) | The Zabbix server's own statistics, plus every item carrying the tag. |
| **Tagged only** | Only items carrying the tag. |
| **All items** | Every enabled item on every selected host. |

> [!WARNING]
> **All items** can create a very large number of entities. A fully templated Zabbix
> host commonly has hundreds, sometimes thousands, of items, because discovery rules
> (filesystems, network interfaces, services, containers…) create one item per
> discovered object. Every item becomes an enabled entity that Home Assistant polls
> and records, which can slow Home Assistant down and grow its database quickly.
> The setup screen shows the actual numbers and asks you to confirm. Only choose it
> if you know what you're doing; tagging is the precise alternative.

### Tagging items in Zabbix

Add a tag named `homeassistant` (any value, or empty) to the items you want. Tags
can be set on:

- an **item** on a host;
- an **item in a template**, so every host using the template gets it;
- an **item prototype** of a discovery rule, so every discovered item gets it (for
  example every filesystem's *Space utilization*).

Changes are picked up within 10 minutes, or immediately when you reload the
integration.

### How items are shown

Items keep their Zabbix names, so entity IDs are the device name plus the item
name: the server item *Zabbix server: Queue* on the Zabbix device becomes
`sensor.zabbix_zabbix_server_queue`, and *Linux: CPU utilization* on host *web-01*
becomes `sensor.web_01_linux_cpu_utilization`. Rename entities in Home Assistant if
you prefer. Their type follows their Zabbix settings:

- **Value map** → a sensor showing the mapped text (e.g. *Up*/*Down*). A value
  without a mapping shows as unknown, with the raw value in the `raw_value`
  attribute.
- **Units** → units and device classes: `B`/`GiB` → data size, `bps`/`Bps` → data
  rate, `s`/`ms` → duration, `°C` → temperature, `%`, `W`, `V`, `A`, `Hz`, `dBm`…
  Other units are shown as-is. Zabbix's `!` no-conversion prefix is removed.
- `unixtime` → a timestamp; `uptime` → the **boot time** as a timestamp.
- **Text** items → text sensors. Home Assistant states are limited to 255
  characters; the full text is in the `raw_value` attribute.
- Items that are *not supported* in Zabbix are unavailable. Binary items are
  skipped.

## Devices and entities

### Host devices

| Entity | Description |
|---|---|
| Agent / SNMP / IPMI / JMX availability | One per interface type the host has, exactly like Zabbix's *Availability* column: `available`, `not_available`, `unknown` or `mixed`. Active agent checks count towards *Agent*. |
| Problem | On when the host has a problem. Attributes list the problems. |
| Highest problem severity | `none`, `not_classified`, `information`, `warning`, `average`, `high`, `disaster`. |
| Maintenance | On while the host is in a maintenance period. |
| Problem events | Event entity, see below. |
| Items | Tagged items (or all items), see above. |

### Zabbix device

| Entity | Description |
|---|---|
| Problems | Number of problems, with per-severity counts and the list of problems. |
| Version | Zabbix server version. |
| Hosts, Items, Not supported items, Triggers | Monitored hosts, and enabled items and triggers on monitored hosts: the *enabled* figures on Zabbix's *System information* page, not its totals (not in *Tagged only* mode). |
| Server items | The server's self-monitoring items (not in *Tagged only* mode). Only the main statistics are enabled by default: processed values per second, the queues (`zabbix[queue]`, `zabbix[queue,10m]`, LLD, preprocessing, connector) and cache usage (`…,pused`). The rest (per-process utilization, per-type value rates, value/trend cache statistics…) are created disabled; enable the ones you want. |
| Problem events | Event entity for every problem. |

The Zabbix server's *machine* (e.g. its Linux VM, if you monitor it) is a normal
host device. The Zabbix device describes the Zabbix *service*, which also works
when Zabbix runs in a container.

### Which problems count

Counts and the *Problem* sensors follow Zabbix's default views (Problems page,
dashboards, host list). Like them, they leave out problems that are:

- **suppressed** (hosts in maintenance);
- **symptoms** of another problem (cause/symptom correlation);
- on a **disabled trigger** or an **unmonitored host** (Zabbix keeps such problems
  open but doesn't show them);
- on a trigger that **depends on another trigger in problem state**.

The *Problems* sensor's `suppressed`, `symptoms` and `hidden` attributes count the
problems left out (`hidden` covers the last two reasons). Problems on hosts outside
the selected host groups still count on the Zabbix device. Problem events fire for
every problem, shown or not.

## Problem events

When a poll finds a change, the integration fires a `zabbix_problem` event and
updates the matching **Problem events** entities. Event types: `problem`,
`resolved`, `acknowledged`, `unacknowledged`, `severity_changed`, `suppressed`,
`unsuppressed`.

Event data: `event_id`, `trigger_id`, `name`, `severity`, `acknowledged`,
`suppressed`, `symptom`, `clock`, `opdata`, `tags`, `host_ids`, `hosts` (and
`config_entry_id` and `type` on the bus event).

Example: notify about new high or disaster problems.

```yaml
automation:
  - alias: Zabbix high problems
    triggers:
      - trigger: event
        event_type: zabbix_problem
        event_data:
          type: problem
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.severity in ['high', 'disaster'] }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "Zabbix: {{ trigger.event.data.hosts | join(', ') }}"
          message: "{{ trigger.event.data.name }}"
```

Example: flash a light while a host has a problem.

```yaml
automation:
  - alias: NAS problem light
    triggers:
      - trigger: state
        entity_id: binary_sensor.nas_problem
        to: "on"
    actions:
      - action: light.turn_on
        target:
          entity_id: light.office
        data:
          color_name: red
          flash: long
```

Polling detects changes, so events arrive within one update interval. Changes that
come and go between two polls are not seen. Real-time delivery through Zabbix
alerting is planned.

## Actions

| Action | Fields |
|---|---|
| `zabbix.acknowledge_problem` | `config_entry_id`, `event_id` (one or more), optional `message` |
| `zabbix.unacknowledge_problem` | `config_entry_id`, `event_id`, optional `message` |
| `zabbix.close_problem` | `config_entry_id`, `event_id`, optional `message`. The trigger must allow manual close. |
| `zabbix.start_maintenance` | `device_id` (host devices), `duration`, optional `collect_data` (default true), `description` |
| `zabbix.end_maintenance` | `device_id`. Ends maintenance periods started from Home Assistant only. |

The API token's user needs the matching permissions (see
[Creating an API token](#creating-an-api-token)). Maintenance periods created by
Home Assistant are named `Home Assistant: <hosts> (<timestamp>)`.

Example: acknowledge a problem from a notification action.

```yaml
action: zabbix.acknowledge_problem
data:
  config_entry_id: 01J...   # pick the Zabbix server in the UI editor
  event_id: "{{ trigger.event.data.event_id }}"
  message: Acknowledged from Home Assistant
```

## How data is updated

- **Every update interval** (default 30 s): host availability and maintenance, the
  latest item values (only for enabled entities) and problems.
- **Every 10 minutes**, and whenever hosts change: the list of items, their names,
  units and value maps, and the server counters. New hosts and items are added and
  removed ones are cleaned up automatically.

## Known limitations

- Pushing Home Assistant states to Zabbix (the built-in integration's feature) is
  not available yet.
- Events are detected by polling, not delivered in real time.
- Zabbix 6.x is not supported (it uses a different API authentication).
- Item names are Zabbix's names, which often start with a template prefix such as
  `Linux:`.
- Custom severity names configured in Zabbix are not used; the standard names are.

## Troubleshooting

- **Invalid API token**: tokens can expire or be disabled; Home Assistant asks for a
  new one automatically.
- **The user is not allowed to use the Zabbix API**: enable *API access* in the
  user's role.
- **No Zabbix API was found**: check the URL; it's the web interface address,
  usually ending in `/zabbix/`.
- **Missing items**: check that the item is enabled, on a monitored host in a
  selected group, and carries the tag (the tag *name* must match). Then reload.
- **Host groups were removed** repair: a selected group no longer exists; update
  the groups under **Configure**.
- Download **diagnostics** from the integration or a device page when reporting an
  issue; the token is redacted.

## Removal

Go to **Settings → Devices & services → Zabbix → ⋮ → Delete**, then remove the
repository from HACS. Nothing is changed in Zabbix, apart from maintenance periods
you created with the actions. The API token and user can then be deleted in Zabbix.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest tests
.venv/bin/ruff check custom_components tests && .venv/bin/mypy
```

The tests run the integration against a fake Zabbix JSON-RPC server. CI also runs
contract tests against the official Zabbix 7.0 and 7.4 Docker images.

## License and trademarks

This integration is licensed under the [MIT License](LICENSE).

This is an independent, community project. It is **not affiliated with, endorsed
by, or sponsored by Zabbix SIA**. Zabbix® and the Zabbix logo are trademarks of
Zabbix SIA and are used here only to identify the software this integration works
with. All other trademarks are the property of their respective owners.
