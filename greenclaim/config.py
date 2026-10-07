from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # .env подхватывается до чтения переменных
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
DATA_DIR = ROOT / "data"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")          # Telegram
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")  # MAX
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "greenclaim.sqlite3"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", ROOT / "tmp_uploads"))
MAX_PHOTOS = 3
