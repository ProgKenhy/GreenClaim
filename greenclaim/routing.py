"""Подбор адресатов по квалификации и геопозиции."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import DATA_DIR
from .geo import nearest_center
from .legal import Qualification

_AGENCIES = json.loads((DATA_DIR / "agencies.json").read_text(encoding="utf-8"))["agencies"]


@dataclass(frozen=True)
class Addressee:
    id: str
    name: str
    short: str
    verified: bool


def resolve(q: Qualification, lat: float, lon: float) -> list[Addressee]:
    city, _ = nearest_center(lat, lon)
    result = []
    for aid in q.agency_ids:
        a = _AGENCIES[aid]
        name, short = a["name"], a["short"]
        if aid == "municipality":
            name = f"Администрация муниципального образования с центром в г. {city}"
            short = f"Администрация ({city})"
        result.append(Addressee(aid, name, short, bool(a.get("verified"))))
    return result
