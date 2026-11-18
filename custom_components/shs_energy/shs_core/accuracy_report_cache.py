"""Retain revision-addressed diagnostics behind the ordinary status interface."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class AccuracyReportCache:
    """One transient cache per API credential; never persist a revision alone.

    Status and runtime requests can overlap. Locally issued request numbers keep
    an older reply from replacing a report accepted from a newer request.
    """

    def __init__(self) -> None:
        self._home_id: str | None = None
        self._revision: str | None = None
        self._report: dict | None = None
        self._error: str | None = None
        self._issued = 0
        self._accepted = 0

    def begin(self) -> tuple[int, str | None]:
        self._issued += 1
        return self._issued, self._revision

    def _clear(self, error: str | None = None) -> None:
        self._revision = None
        self._report = None
        self._error = error

    def apply(self, status: dict[str, Any], request: int) -> dict[str, Any]:
        result = dict(status)
        state = result.pop('forecast_accuracy_state', None)
        revision = result.pop('forecast_accuracy_revision', None)
        if state is None and 'forecast_accuracy' not in status:
            return result
        home = status.get('home_id')
        if request < self._accepted:
            if home != self._home_id:
                result.update(forecast_accuracy=None, forecast_accuracy_error='Accuracy unavailable')
                return result
        else:
            self._accepted = request
            if home != self._home_id:
                self._clear()
                self._home_id = home
            report = status.get('forecast_accuracy')
            error = status.get('forecast_accuracy_error')
            if error:
                self._clear(error)
            elif state == 'changed' and isinstance(revision, str) and revision and isinstance(report, dict):
                self._revision = revision
                self._report = deepcopy(report)
                self._error = None
            elif state == 'unchanged' and revision == self._revision and self._report is not None:
                self._error = None
            elif state == 'missing':
                self._clear()
            elif state is None:
                # Reader-first rollout: a server without revision delivery still
                # returns the full report, and no token is sent back to it.
                self._revision = None
                self._report = deepcopy(report) if isinstance(report, dict) else None
                self._error = None
            else:
                self._clear('Accuracy unavailable')
        result.update(forecast_accuracy=deepcopy(self._report), forecast_accuracy_error=self._error)
        return result
