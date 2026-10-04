# Minimum run time retired

Decision, 4 October 2026: remove SHS minimum-on/off settings and hard elapsed-time run locks for every appliance, including the live pool heater's four-hour setting. The current planner and integration no longer create or enforce run-duration promises.

Configuration version 15 removes the old timer fields through the existing one-time migration. The app-owned settings record is retired through its ordinary acknowledged revision writer on startup, including the live four-hour pool setting. The ownership decoder discards persisted `runs` while retaining captured settings and ownership. Old snapshots carrying a timer no longer force heating, charging, or hot-water permission.

Native equipment protections remain in the equipment. A running pool may still have an economically preferred continuation candidate, compared against the freely replanned schedule using the existing release margin. That preference is not an actuator timer.

Restart, source loss, stale plans and faults retain the last setting sent. Leaving Controlling, including for Verification, still restores captured settings; no SHS run timer delays that handover. See [control continuity](control-continuity.md).

Historical implementation: minimum runs were restored on 25 September in both repositories, including a four-hour pool minimum. This retirement reverses that duration requirement without reversing the later control-continuity corrections.
