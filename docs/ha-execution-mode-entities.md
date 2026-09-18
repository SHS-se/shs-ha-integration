# Execution mode entities and controller details

The integration publishes one configuration `select` for each Planned, Included
device shown in the Schedule tab, including the house battery. Each is named
“<device name> execution mode” and belongs to the existing SHS integration device.
The two options are displayed as **Verification** and **Controlling**.

The entity uses the same permission checks and saved setting as the configuration
panel. Controlling is rejected with the same explanation when setup, website
choices or the current plan do not permit it. Switching an admitted device back
to Verification remains possible when planning is unavailable. Changing mode
requests a fresh plan and invokes the existing controller handover path.

Automations use Home Assistant's `select.select_option` action. Select the actual
entity created for your device; the ID below is an example:

```yaml
action: select.select_option
target:
  entity_id: select.smart_home_solutions_house_battery_execution_mode
data:
  option: control_verification
```

Use `controlling` to enable execution. Automation values are stable identifiers;
they are not the translated display labels. This follows Home Assistant's
[select entity interface](https://developers.home-assistant.io/docs/core/entity/select/).

Changing a device to Monitoring on the website removes its select when the new
website configuration reaches the integration. Excluding it locally also removes
its select, including its entity-registry entry. The existing website refresh can
bring the change in immediately. Temporary lack of a plan does not remove a
Planned device; the select remains available to switch to Verification and reports
why Controlling is blocked. Re-admission follows the existing rule of starting in
Verification. User changes to an entity's name or ID are not retained after its
registry entry is removed.

Every existing controller status sensor—battery, pool, EV, and the aggregate other
devices sensor—now exposes a plain-language `explanation` attribute. The aggregate
sensor also includes an explanation inside each device's details. Each mode select
exposes its own device's explanation and `controlling_blocked_reason`.

The battery explanation shares the card's live wording, loss-model description,
planned action, stored-energy difference and next action. If present, the target
deadline is a separate `plan_target_deadline` timestamp. Other controllers describe
their own current plan and available measured values; they do not invent battery
accounting or infer physical delivery from a command acknowledgement. Existing
technical attributes remain available.

The select lifecycle, shared correction path, mode changes and sensor details have
regression coverage using fake HA ports. Hardware control is not exercised by
these tests. The standalone test/replay import paths preserve Python's standard
library precedence so the HA `select.py` platform cannot shadow Python's native
`select` module.
