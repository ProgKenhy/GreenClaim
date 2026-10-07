"""SQLite-хранилище. Персональные данные заявителя НЕ сохраняются:
только обезличенная запись о нарушении (для будущей B2B-карты эко-рисков)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    district    TEXT,
    tags        TEXT NOT NULL,
    norms       TEXT NOT NULL,
    agencies    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_scenario ON reports(scenario);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with closing(self._conn()) as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def add_report(self, *, scenario, lat, lon, district, tags, norms, agencies) -> int:
        with closing(self._conn()) as c, c:
            cur = c.execute(
                "INSERT INTO reports(created_at,scenario,lat,lon,district,tags,norms,agencies) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), scenario, lat, lon,
                 district, json.dumps(sorted(tags), ensure_ascii=False),
                 json.dumps(norms, ensure_ascii=False), json.dumps(agencies)),
            )
            return cur.lastrowid

    def count(self) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]

    def export_geojson(self, scenario: str | None = None) -> dict:
        q, args = "SELECT * FROM reports", ()
        if scenario:
            q, args = q + " WHERE scenario=?", (scenario,)
        with closing(self._conn()) as c:
            rows = c.execute(q, args).fetchall()
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {"id": r["id"], "created_at": r["created_at"],
                               "scenario": r["scenario"], "district": r["district"],
                               "tags": json.loads(r["tags"])},
            } for r in rows],
        }
