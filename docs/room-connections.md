# Room connections belong to the SHS app

The app's **Settings → Rooms** editor adds, changes and removes undirected
doorways. **Save connections** persists the whole map through the app's existing
configuration writer. Discard and conflict recovery reload saved settings.
Connections involving a removed Home Assistant room remain visible and removable.
Room connections do not change device permissions or release equipment.

`configuration.json` owns `room_connections`, containing a local revision and
canonical room pairs. An absent document means existing connections have not yet
been recovered; an empty list means the user has saved an empty map. The first
successful recovery copies the controlling backend's existing map once. It never
combines it with an observing backend's map or replaces an adopted local document.
Recovery failure leaves planning and execution running and retries independently.

Each paired backend receives the same app-owned document through the device-token
`energy-room-connections` endpoint. This settings exchange does not depend on
subscription entitlement or temperature statistics. The local document is the
durable pending publication; restarting resends it idempotently. A database row
lock checks the stable app installation owner and map revision, preventing a
delayed request from restoring a deleted connection. An owner mismatch or older
restored local record is reported for explicit repair rather than taking over.

The website's `energy_room_profiles.connections` is a replica used by the existing
room learners and forecasts. The portal has no map editor or map write permission.
Temperature-only uploads continue updating the room catalogue and readings without
changing the saved map or its publication revision.

Deploy the website migration and paired endpoint to dev and verify them before
production promotion. Deploy that backend before installing app `0.1.0-beta.221`.
Until the endpoint is available, the app retains existing connections in the
backend and retries recovery; it must never manufacture an empty local map.
