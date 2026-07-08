"""Watch many agent sessions at once — each one is a "station".

A Station is one open session file: its own handle, read buffer, parser
state, event batch and activity clock. The StationMux discovers active
sessions across all sources (Claude Code, Codex, ...), opens each at its
live edge, reads every handle each tick, adds stations as new sessions
appear, and retires stations that have gone stale.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO


@dataclass
class Station:
    source: object
    path: Path
    label: str
    file: TextIO
    state: dict
    buf: str = ""
    last_activity: float = field(default_factory=time.monotonic)
    idle_announced: bool = False
    batch: list = field(default_factory=list)
    batch_started: float = 0.0

    @property
    def key(self) -> str:
        return str(self.path)

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


class StationMux:
    def __init__(
        self,
        sources: list,
        max_stations: int = 6,
        active_window: float = 600.0,
        stale_timeout: float = 900.0,
        rescan_interval: float = 3.0,
        evict_idle: float = 45.0,
    ) -> None:
        self.sources = sources
        self.max_stations = max_stations
        self.active_window = active_window
        self.stale_timeout = stale_timeout
        self.rescan_interval = rescan_interval
        # When all slots are full, a station this quiet can lose its slot to a
        # newer session. Without this, a session that happened to be busy at
        # startup squats on a slot for the whole stale_timeout while a live
        # agent goes unnarrated.
        self.evict_idle = evict_idle
        self.stations: list[Station] = []
        self._last_scan = 0.0

    # -- discovery ---------------------------------------------------------

    def _candidates(self) -> list[tuple[float, object, Path]]:
        found = []
        for src in self.sources:
            for p in src.discover():
                try:
                    found.append((os.path.getmtime(p), src, p))
                except OSError:
                    continue
        found.sort(key=lambda t: t[0], reverse=True)
        return found

    def _open_station(self, src, path: Path, at_end: bool = True) -> Station:
        f = open(path, encoding="utf-8", errors="replace")
        if at_end:
            f.seek(0, os.SEEK_END)
        base = src.project_name(path)
        label, n = base, 2
        while any(s.label == label for s in self.stations):
            label = f"{base} {n}"
            n += 1
        return Station(source=src, path=path, label=label, file=f,
                       state=src.new_state())

    def rescan(self, force: bool = False) -> tuple[list[Station], list[Station]]:
        """Refresh the station list. Returns (added, removed)."""
        now = time.monotonic()
        if not force and now - self._last_scan < self.rescan_interval:
            return [], []
        self._last_scan = now

        wall = time.time()
        removed = []
        for st in list(self.stations):
            try:
                mtime = os.path.getmtime(st.path)
                dead = wall - mtime > self.stale_timeout
            except OSError:
                dead = True  # file vanished
            if dead:
                st.close()
                self.stations.remove(st)
                removed.append(st)

        open_paths = {st.path for st in self.stations}
        added = []
        for mtime, src, path in self._candidates():
            if path in open_paths or wall - mtime > self.active_window:
                continue
            if len(self.stations) >= self.max_stations:
                # Full house: evict the quietest station, but only if it has
                # been idle a while AND this newcomer is genuinely fresher.
                victim, victim_mtime = None, mtime
                for st in self.stations:
                    try:
                        m = os.path.getmtime(st.path)
                    except OSError:
                        m = 0.0
                    if m < victim_mtime and wall - m >= self.evict_idle:
                        victim, victim_mtime = st, m
                if victim is None:
                    continue  # every station is busier than the newcomer
                victim.close()
                self.stations.remove(victim)
                removed.append(victim)
            st = self._open_station(src, path)
            self.stations.append(st)
            added.append(st)
        return added, removed

    def pin(self, src, path: Path) -> Station:
        """Force-open one session regardless of activity (startup fallback)."""
        st = self._open_station(src, path)
        self.stations.append(st)
        return st

    def newest_any(self) -> Optional[tuple[object, Path]]:
        cands = self._candidates()
        if not cands:
            return None
        _, src, path = cands[0]
        return src, path

    # -- reading -----------------------------------------------------------

    def read_all(self) -> list[tuple[Station, dict]]:
        """One non-blocking pass over every station; parsed raw events out."""
        out: list[tuple[Station, dict]] = []
        for st in self.stations:
            try:
                chunk = st.file.read()
            except (OSError, ValueError):
                continue  # will be retired by the next rescan
            if not chunk:
                continue
            st.buf += chunk
            *complete, st.buf = st.buf.split("\n")
            for line in complete:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append((st, json.loads(line)))
                except json.JSONDecodeError:
                    continue
        return out

    def close(self) -> None:
        for st in self.stations:
            st.close()
        self.stations.clear()
