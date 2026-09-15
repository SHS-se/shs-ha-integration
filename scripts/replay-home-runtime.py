#!/usr/bin/env python3
"""Replay explicit offline events and simulated durable writes; never access devices."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "shs_energy"))
from home_runtime import JournalDurable, Persist, reduce_home, reservation, Send, authorize_send
from home_runtime_checkpoint import (
    MAX_BYTES, decode_checkpoint, decode_event, effects_json, encode_checkpoint,
    read_runtime_json, restore_checkpoint,
)


def replay(value):
    if (type(value) is not dict or set(value) != {"schema_version", "initial", "records"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["records"]) is not list or not 0 < len(value["records"]) <= 10000):
        raise ValueError("expected a version-1 trace with 1..10000 records")
    state = decode_checkpoint(json.dumps(value["initial"]).encode())
    durable = encode_checkpoint(state)
    durable_revision = state.revision
    pending = {}
    rows = []
    for index, record in enumerate(value["records"]):
        if type(record) is not dict or type(record.get("at_ms")) is not int or record["at_ms"] < 0:
            raise ValueError(f"record {index}: invalid time")
        now = record["at_ms"]
        if set(record) == {"at_ms", "event"}:
            state, effects = reduce_home(state, decode_event(record["event"]), now)
        elif set(record) in ({"at_ms", "action"}, {"at_ms", "action", "revision"}) and record["action"] == "durable":
            revision = record.get("revision", max(pending, default=-1))
            if type(revision) is not int or revision not in pending or revision < durable_revision:
                raise ValueError(f"record {index}: no ordered pending journal revision")
            durable = pending[revision]
            durable_revision = revision
            state, effects = reduce_home(state, JournalDurable(revision), now)
            pending = {key: data for key, data in pending.items() if key > revision}
        elif set(record) == {"at_ms", "action"} and record["action"] == "restart":
            state, effects = restore_checkpoint(durable, now)
            pending = {}
        else:
            raise ValueError(f"record {index}: unsupported fields/action")
        for effect in effects:
            if isinstance(effect, Send) and not authorize_send(state, effect, now):
                raise ValueError(f"record {index}: final dispatch authorization failed")
            if isinstance(effect, Persist):
                pending[effect.state.revision] = encode_checkpoint(effect.state)
        rows.append({
            "record": index, "at_ms": now, "revision": state.revision,
            "policy": None if state.policy is None else {
                "revision": state.policy.revision, "selected_id": state.policy.selected_id,
                "status": state.policy.status, "from_ms": state.policy.compiled.summary.from_ms,
                "until_ms": state.policy.compiled.summary.until_ms},
            "effects": [{"type": "Persist", "revision": e.state.revision} if isinstance(e, Persist)
                        else effects_json((e,))[0] for e in effects],
            "groups": [{"id": g.spec.id, "status": g.status, "mode": g.mode,
                        "attempts": [{"id": a.id, "stage": a.stage} for a in g.attempts],
                        "reservation_w": {"import": reservation(g, state.last_time_ms).import_w,
                                          "export": reservation(g, state.last_time_ms).export_w}}
                       for g in state.groups],
            "actuals": None if state.ledger is None else [
                {"stream_id": stream.spec.stream_id, "boundary_id": stream.spec.boundary_id,
                 "direction": stream.spec.direction, "started_at_ms": stream.started_at_ms,
                 "retained_from_ms": stream.samples[0].at_ms if stream.samples else None,
                 "through_ms": stream.samples[-1].at_ms if stream.samples else None,
                 "since_first_anchor_mwh": None if not stream.samples else {
                     "lower": stream.lifetime.lower_mwh, "upper": stream.lifetime.upper_mwh}}
                for stream in state.ledger.streams],
        })
    return {"schema_version": 1, "rows": rows, "final_checkpoint": json.loads(encode_checkpoint(state))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        with args.trace.open("rb") as stream:
            value = read_runtime_json(stream.read(MAX_BYTES + 1))
        result = replay(value)
    except (ValueError, OSError, RecursionError) as error:
        parser.exit(2, f"Invalid runtime trace: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
