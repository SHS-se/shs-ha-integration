# 0.1.0-beta.5

Bundle preparation companion 0.9.0-beta.53. Extract shared device ownership,
minimum-run rules and native execution; recheck native hardware metadata after
persisting a command and before dispatch. Add a verified dormant importer for
cold exports with accounting parity, immutable retry evidence and exact target
pair binding. This is preparation: the integration still owns control, and the
remote runtime and activation are not yet available. Existing SHS branding is
unchanged.

# 0.1.0-beta.4

Bundle preparation companion 0.9.0-beta.52 with durable command outcomes for all
actuators and a process-lifetime migration fence. Verify the existing shutdown
sample flush before recording a clean stop. Add
a verified cold-export worker for the eventual one-off cutover. The integration
remains the controller; do not export the live household until the app runtime
and importer are ready. Existing SHS branding is unchanged.

# 0.1.0-beta.3

Package the shared runtime/accounting and SQLite readers independently of Home
Assistant. Add a one-off read-only database rehearsal tool with integrity and
accounting checks. The integration remains the sole controller; this release
does not activate data migration. Bundled companion: 0.9.0-beta.51.

# 0.1.0-beta.2

Keep companion staging and backups outside Home Assistant's integration discovery
directory. Fixes beta.49 appearing to remain installed and SHS failing to load after
the beta.50 companion installation. The bundled integration remains beta.50.

For installations affected by beta.1, move
`/config/custom_components/.shs_energy-before-app` outside `custom_components`, then
restart Home Assistant Core. Keep the backup; no integration data needs changing.

# 0.1.0-beta.1

First installable HAOS app: existing SHS branding, authenticated Ingress, five-screen
dashboard, ECharts forecast schedule, controller observations, container/SQLite
diagnostics and explicit installation of companion 0.9.0-beta.50. Read-only device
observation; runtime/data migration remains disabled.
