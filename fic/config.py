"""Config loading: config/fic.toml + config/company_profile.toml + .env (no secrets required)."""
import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("FIC_ROOT", Path(__file__).resolve().parent.parent))


def _load_env(path: Path):
    # Minimal .env reader so we don't need python-dotenv. Existing env vars win.
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load(config_path=None, profile_path=None):
    _load_env(ROOT / ".env")
    cfg = tomllib.loads(Path(config_path or os.environ.get("FIC_CONFIG", ROOT / "config/fic.toml")).read_text())
    prof_file = Path(profile_path or os.environ.get("FIC_PROFILE", ROOT / "config/company_profile.toml"))
    cfg["profile"] = tomllib.loads(prof_file.read_text()) if prof_file.exists() else {}
    db = os.environ.get("FIC_DB") or cfg.get("db_path", "data/fic.db")
    cfg["db_path"] = str(db if Path(db).is_absolute() else ROOT / db)
    w = cfg["scoring"]["weights"]
    total = sum(w.values()) or 1
    cfg["scoring"]["weights"] = {k: v / total for k, v in w.items()}
    return cfg
