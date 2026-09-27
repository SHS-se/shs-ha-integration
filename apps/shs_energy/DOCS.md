# Install SHS Energy

1. In **Settings → Apps → App store → Repositories**, add
   `https://github.com/SHS-se/shs-ha-integration`.
2. Install **SHS Energy**, then start it and choose **Open web UI**.
3. Enable **Show in sidebar** on the app’s Info page if desired. The entry is titled
   **SHS**, using the integration’s existing home-and-energy glyph. The SHS shield
   appears in the app store, app details, web header and browser icon.
4. On the app’s **Configuration** tab enable **Install companion**, save and restart
   the SHS app. Check the installation result in the web UI’s **Settings** screen
   or the app log.
5. Restart **Home Assistant Core** to load the companion. The app requires the exact
   bundled integration version, **0.9.0-beta.50**. Once connected, turn off
   **Install companion** and save.

If SHS is new to this home, add **Smart Home Solutions Energy** in
**Settings → Devices & services** after restarting Core and complete its setup.
An existing SHS configuration is preserved automatically.

## What this beta does

- Overview, Schedule, Controller, System and Settings screens with light/dark themes.
- Five linked ECharts panels, forecast scope selection, interval inspection and a
  table view. Published and estimated prices stay identifiable in the inspector.
- Latest integration decisions and current physical readings, clearly distinguished.
- One-minute measured/forecast comparisons captured since installation.
- Container CPU, memory, I/O and network counters; SQLite sizes, row counts, named
  operation timings and the history query plan.
- Seven days of diagnostic samples; the dashboard displays the latest 240 samples.
- Local Ingress access only. No host port, browser token or external CDN is needed.

The application starts collecting its own diagnostic history on first run. It does
not claim to have measured history from before installation, actual-cost accounting,
or an explanation of every historical controller action.

## Companion installation and compatibility

The container includes the full integration for this first release. The thin gateway
and runtime extraction follow in the migration phase. Live updates require protocol
1 and the exact bundled integration version. A mismatch stops dashboard updates;
it does not stop the existing Home Assistant controller.

Installation is an explicit Home Assistant administrator setting, never an automatic
side effect of opening the web page. The installer accepts a clean beta.49, an absent
integration, or already matching beta.50 files. It checks file hashes, stages the
replacement and journals the swap. It retains the previous files at
`/config/custom_components/.shs_energy-before-app` inside Home Assistant. It never
changes `.storage`, configuration entries or integration databases. An interrupted
swap resumes forward on the next app start.

If local edits or an unsupported release are detected, installation stops and reports
why. Update through HACS to the exact bundled version, or review those edits before
retrying. Do not delete an installation journal or backup without inspecting it.
HACS remains able to update the integration; installing a different version will
produce a visible compatibility mismatch until a matching app release is installed.

## Data migration

**Migration is not enabled in this beta.** The existing integration remains the sole
controller and keeps all its data. The runtime handover and one-off database transfer
will follow the documented migration protocol after this app has been verified on
HAOS. Starting or stopping this app does not transfer controller ownership.

## Permissions and troubleshooting

The app accesses the Home Assistant API for read-only snapshots and its own Supervisor
statistics. The Home Assistant configuration directory is writable solely to support
the explicit companion installation option. Keep this option off after installation.

- **HTTP 404 / awaiting connection:** install the companion and restart Core.
- **Version mismatch:** compare loaded and bundled versions in Settings.
- **Integration not loaded:** inspect SHS in Devices & services and Core logs.
- **Unavailable readings:** verify the actual measurement sources in SHS configuration.
- **Install failed:** read the precise message in Settings or the app log; existing
  files and data are not silently replaced.

Resource counters come from Supervisor and reset on container restart. Filesystem
free space is shared storage, not a private disk allocation. Database statistics cover
the app’s own database; the existing integration databases have not moved.
