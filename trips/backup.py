import sqlite3
from datetime import datetime
from pathlib import Path


def backup_sqlite(db_path, dest_dir, keep=7, now=None):
    """Copie une base SQLite de façon sûre (même si le site écrit en même temps),
    vérifie la copie, et ne garde que les `keep` sauvegardes les plus récentes."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Base introuvable : {db_path}")
    if keep < 1:
        raise ValueError("keep doit valoir au moins 1")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"db-{stamp}.sqlite3"

    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
        source.close()
    if result != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"La copie est corrompue ({result}), elle a été supprimée.")

    dest.chmod(0o600)
    for old in sorted(dest_dir.glob("db-*.sqlite3"))[:-keep]:
        old.unlink()
    return dest
