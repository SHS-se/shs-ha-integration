# Battery configuration changes — 11 September 2026

This records the current configuration work. Integration/device selection remains
an unimplemented proposal in [the catalog notes](device-catalog-design.md).

## Customer-facing changes

- Rated capacity and physical power/cutoff limits accept a sensor or a number.
  Sensor selections display the source's unit; literal power inputs use watts.
- Maximum charge is removed from configuration. The planner still has the physical
  full-charge boundary of 100%; a retired saved ceiling does not override it.
- The measurement-sign and command-sign toggles are removed. Charging and
  discharging binary sensors establish measured direction; the magnitude comes
  from the power sensor, so either signed or inverted measurements can be used.
  Contradictory direction observations are reported rather than guessed.
- Command modes, including baseline, are selected from the actual EMS selector
  options and validated on save and before execution. This prevents invalid
  strings; it does not establish the physical semantics of an unfamiliar mode.
- Optional fields have a Remove action. Saving removal retains an explicit empty
  value so an initial default cannot silently replace it on reload. Required
  fields cannot be hidden; clearing a planning input that is needed must be
  resolved before another plan can be built. Editable optional fields remain
  available through Add a setting. Charger phases/voltage remain internal.

## Command contract

The signed actuator target is replaced with separate non-negative charge and
discharge limit entities. Their units and legal range/step come from the entities.
Before taking ownership, both existing normal limits must be finite, legal and
restorable. An unset sentinel such as `4294967.295` is rejected before any write.

A mode transition closes both ceilings before selecting the new mode and opening
the requested ceiling. Repeating the same request does not cycle the limits.
Accepted settings are confirmed separately from fresh direction/power observations.
A ceiling is not an exact-power promise: lower delivered power is reported as
limited. Restoration uses the recorded normal limits and the selected baseline;
a new power observation is required and may legitimately be nonzero.

Migration removes retired keys, moves binary-sensor IDs entered in the old mode
fields into the observation fields, and disables the former battery controller
permission. The former signed-command mapping does not authorize the new limit
controls. A saved old command journal without a pair of normal limits remains a
visible restoration fault; it is not translated into speculative actuator writes.

## Limits of this change

These are configuration and bounded execution changes, not completed Sigenergy
commissioning. Binary direction sensors may derive from the same power reading;
they are not necessarily independent physical evidence. Mode-specific behavior,
plant behavior during outages, and transition timing still need installation
validation. The executable plan still carries charge/discharge watts rather than
the full source/destination intent described by the battery-control design. This
change does not claim to implement that entire design or authorize live control.
