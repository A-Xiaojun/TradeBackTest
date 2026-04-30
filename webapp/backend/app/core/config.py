from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = BASE_DIR / "webapp" / "backend"
DB_FILE = BACKEND_DIR / "trade_view.db"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
