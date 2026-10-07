"""Геоданные: GPS из EXIF, проверка региона, ближайший муниципальный центр."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from PIL import Image

try:  # HEIC/HEIF с iPhone (необязательная зависимость)
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from .config import DATA_DIR

_GPS_IFD = 0x8825
_EXIF_IFD = 0x8769


@dataclass(frozen=True)
class PhotoMeta:
    lat: float | None
    lon: float | None
    taken_at: datetime | None


def _to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    val = d + m / 60 + s / 3600
    return -val if str(ref).upper() in ("S", "W") else val


def read_photo_meta(src: str | Path | IO[bytes]) -> PhotoMeta:
    """Достаёт GPS и время съёмки. Telegram вырезает EXIF из «сжатых» фото,
    поэтому бот просит присылать снимок как файл (документ)."""
    try:
        with Image.open(src) as img:
            exif = img.getexif()
            gps = exif.get_ifd(_GPS_IFD)
            lat = lon = None
            if gps and 2 in gps and 4 in gps:
                lat = _to_deg(gps.get(2), gps.get(1))
                lon = _to_deg(gps.get(4), gps.get(3))
            raw_dt = exif.get_ifd(_EXIF_IFD).get(0x9003) or exif.get(0x0132)
            taken = None
            if raw_dt:
                try:
                    taken = datetime.strptime(str(raw_dt), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
            return PhotoMeta(lat, lon, taken)
    except Exception:
        return PhotoMeta(None, None, None)


_districts = json.loads((DATA_DIR / "districts.json").read_text(encoding="utf-8"))
_BBOX = _districts["bbox"]
_CENTERS = _districts["centers"]


def in_region(lat: float, lon: float) -> bool:
    b = _BBOX
    return b["lat_min"] <= lat <= b["lat_max"] and b["lon_min"] <= lon <= b["lon_max"]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def nearest_center(lat: float, lon: float) -> tuple[str, float]:
    """(город-центр, расстояние в км). MVP-приближение вместо полигонов МО."""
    best = min(_CENTERS, key=lambda c: _haversine_km(lat, lon, c["lat"], c["lon"]))
    return best["city"], _haversine_km(lat, lon, best["lat"], best["lon"])


def yandex_map_url(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"
