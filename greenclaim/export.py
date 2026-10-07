"""CLI: выгрузка обезличенных обращений в GeoJSON (основа B2B-тепловой карты).
    python -m greenclaim.export [dump|logging] > reports.geojson
"""
import json
import sys

from .config import DB_PATH
from .storage import Storage

if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(Storage(DB_PATH).export_geojson(scenario), ensure_ascii=False, indent=2))
