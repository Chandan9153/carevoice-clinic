"""In-memory live feed: transcript lines and booking events per call.

The Pipecat pipeline and the text demo agent both write here; the
dashboard polls /api/feed?session_id=... once a second during a call.
"""
from __future__ import annotations

import time
from collections import defaultdict

MAX_LINES = 200


class Hub:
    def __init__(self):
        self._lines: dict[str, list[dict]] = defaultdict(list)
        self._events: dict[str, list[dict]] = defaultdict(list)

    def add(self, session_id: str, who: str, text: str) -> None:
        lines = self._lines[session_id]
        lines.append({"who": who, "text": text, "at": time.time()})
        del lines[: -MAX_LINES:]

    def event(self, session_id: str, kind: str, data: dict) -> None:
        self._events[session_id].append({"kind": kind, "data": data, "at": time.time()})

    def feed(self, session_id: str) -> dict:
        return {"transcript": self._lines.get(session_id, []),
                "events": self._events.get(session_id, [])}

    def clear(self, session_id: str) -> None:
        self._lines.pop(session_id, None)
        self._events.pop(session_id, None)


hub = Hub()
