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
