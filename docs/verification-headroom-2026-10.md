# Verification handover and battery relief

17 October 2026. The supplied controller diagnostics show the EV entering
Verification at 10:18 Europe/Stockholm. Its captured baseline was 9 A with the
charger disabled. Restoring that current from 16 A repeatedly failed with
“Waiting for battery charging to release grid capacity”, despite the charger
already being off. The battery simultaneously reported `physical_scope_blocked`.

The shared physical executor now distinguishes writes that add no demand:
switch-off commands and EV current settings while the charger is observed off
do not request grid capacity. The check runs again immediately before dispatch.
Switch-on commands still require reservation and HA's final battery-response
check. Explicit mode exit still restores the captured settings, and failures,
restarts and unavailable plans still hold the last setting.

The battery's relief proof previously used only the native ceiling's calculated
envelope. The runtime observation also includes actual measured flow. If that
flow exceeded the calculated ceiling, even slightly, the exact observation
comparison rejected the ceiling reduction needed to release capacity. The first
native transition now carries both measured and configured bounds in its
transient envelope and relief proof. This allows a ceiling reduction without
claiming the physical response has already fallen. Starting another appliance
still waits for a zero charge ceiling and measured charging at or below the
existing 100 W response threshold.

Synthetic regressions reproduce both failures, cover restoration to an enabled
baseline, and change the charger switch during durable command preparation to
verify the final recheck. The supplied household captures remain outside Git.

Validation: 1,198 integration tests, 150 app tests and 89 frontend configuration
tests passed. Compilation, packaged-app checks and frontend build passed.
Website lint passed with 27 existing warnings; all local mocked-backend browser
suites passed (75 tests, one existing skip). No deployment or physical hardware
test is implied by these checks.
