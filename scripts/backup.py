"""Database backup script for SQLite and PostgreSQL with retention policy.

Keeps the most recent N backups (default: 7) and deletes older ones.
Can be invoked manually or scheduled via cron / task scheduler.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings


def backup_sqlite(db_path: str, backup_dir: Path) -> Path:
    """Safely back up an SQLite database using sqlite3.Connection.backup."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest_path = backup_dir / f"ledger_backup_{timestamp}.db"

    src_con = sqlite3.connect(db_path)
    dst_con = sqlite3.connect(str(dest_path))
    try:
        with dst_con:
            src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    return dest_path


def backup_postgres(db_url: str, backup_dir: Path) -> Path:
    """Back up PostgreSQL database using pg_dump."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest_path = backup_dir / f"ledger_backup_{timestamp}.sql"

    cmd = ["pg_dump", db_url, "-f", str(dest_path)]
    subprocess.run(cmd, check=True)
    return dest_path


def prune_old_backups(backup_dir: Path, keep: int = 7) -> list[Path]:
    """Delete backups older than the most recent `keep` files."""
    if not backup_dir.exists():
        return []

    backups = sorted(
        [p for p in backup_dir.glob("ledger_backup_*.*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    deleted = []
    for old_file in backups[keep:]:
        old_file.unlink()
        deleted.append(old_file)

    return deleted


def run_backup(keep: int = 7, backup_dir: str = "backups") -> Path:
    bdir = Path(backup_dir)
    db_url = settings.database_url

    if db_url.startswith("sqlite"):
        # Strip sqlite:/// or sqlite://
        clean_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        # If relative or in-memory
        if clean_path == ":memory:" or not clean_path:
            clean_path = "./ledger.db"
        if not os.path.exists(clean_path):
            # Create empty if it doesn't exist yet
            Path(clean_path).touch()
        dest = backup_sqlite(clean_path, bdir)
    elif db_url.startswith("postgres"):
        dest = backup_postgres(db_url, bdir)
    else:
        raise ValueError(f"Unsupported database scheme in URL: {db_url}")

    pruned = prune_old_backups(bdir, keep=keep)
    print(f"Backup created: {dest}")
    if pruned:
        print(f"Pruned {len(pruned)} old backup(s): {[p.name for p in pruned]}")
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BusinessPadi database backup utility")
    parser.add_argument("--keep", type=int, default=7, help="Number of recent backups to keep")
    parser.add_argument("--dir", type=str, default="backups", help="Target backup directory")
    args = parser.parse_args()

    run_backup(keep=args.keep, backup_dir=args.dir)
