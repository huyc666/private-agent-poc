"""一次性 SQLite → PostgreSQL 迁移工具（PLAN 6.4）。

迁移前强制备份两个 SQLite 源库；目标库任一业务表已有数据时拒绝执行，
避免隐式合并或覆盖。迁移完成后回填 identity 序列。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import psycopg

from server import db

STATE_TABLES = (
    "messages", "session_meta", "approvals", "api_users", "auth_tokens",
    "login_fails", "audit_log", "session_locks", "workflow_instances",
    "workflow_schedules", "workflow_trigger_attempts", "disabled_packs",
)
USAGE_TABLES = ("usage_records", "run_records")
TABLE_SOURCES = {table: "state" for table in STATE_TABLES}
TABLE_SOURCES.update({table: "usage" for table in USAGE_TABLES})
IDENTITY_TABLES = ("messages", "audit_log", "usage_records")
COPY_BATCH_SIZE = 500


def _backup(path: Path) -> Path:
    backup_path = path.with_name(
        f"{path.name}.pre-pg-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    source = sqlite3.connect(str(path))
    try:
        destination = sqlite3.connect(str(backup_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return backup_path


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(
        f"PRAGMA table_info({table})").fetchall()]


def _assert_sources(paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        if not path.is_file():
            raise RuntimeError(f"{name} SQLite 源库不存在")
        connection = sqlite3.connect(str(path))
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [
                table for table, source in TABLE_SOURCES.items()
                if source == name and table not in tables]
            if missing:
                raise RuntimeError(
                    f"{name} SQLite 源库缺少表：{', '.join(missing)}")
        finally:
            connection.close()


def _assert_empty_target(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()"
            f" AND table_name = ANY(%s)", (list(db.PG_TABLES),))
        existing = {row[0] for row in cursor.fetchall()}
        for table in db.PG_TABLES:
            if table not in existing:
                raise RuntimeError(f"PostgreSQL 目标库缺少表：{table}")
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            (count,) = cursor.fetchone()
            if count:
                raise RuntimeError("PostgreSQL 目标库已有数据，拒绝覆盖迁移")


def _copy_table(target: psycopg.Connection, source: sqlite3.Connection,
                table: str) -> None:
    columns = _columns(source, table)
    placeholders = ", ".join(["%s"] * len(columns))
    with target.cursor() as cursor:
        rows_cursor = source.execute(
            f"SELECT {', '.join(columns)} FROM {table}")
        while rows := rows_cursor.fetchmany(COPY_BATCH_SIZE):
            cursor.executemany(
                f"INSERT INTO {table} ({', '.join(columns)})"
                f" VALUES ({placeholders})", rows)


def migrate(state_source: str | Path, usage_source: str | Path,
            target_url: str, *, assume_yes: bool = False) -> None:
    paths = {
        "state": Path(state_source),
        "usage": Path(usage_source),
    }
    _assert_sources(paths)
    backups = {name: _backup(path) for name, path in paths.items()}
    if not assume_yes:
        print("将迁移以下源库，并已生成备份：")
        for name, backup in backups.items():
            print(f"  {name}: {backup}")
        if input("确认向当前为空的目标 PostgreSQL 执行迁移？输入 yes 执行：").strip() != "yes":
            raise RuntimeError("用户未确认迁移")

    with psycopg.connect(target_url) as target:
        try:
            db.ensure_postgres_schema(target)
            _assert_empty_target(target)
            for table in db.PG_TABLES:
                source_name = TABLE_SOURCES[table]
                source = sqlite3.connect(str(paths[source_name]))
                try:
                    _copy_table(target, source, table)
                finally:
                    source.close()
            with target.cursor() as cursor:
                for table in IDENTITY_TABLES:
                    cursor.execute(
                        "SELECT setval(pg_get_serial_sequence(%s, 'id'),"
                        " COALESCE(MAX(id), 0) + 1, false)"
                        f" FROM {table}", (table,))
            target.commit()
        except Exception:
            target.rollback()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="一次性 SQLite → PostgreSQL 迁移")
    parser.add_argument("--state-source", required=True)
    parser.add_argument("--usage-source", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    migrate(args.state_source, args.usage_source, args.target_url,
            assume_yes=args.yes)


if __name__ == "__main__":
    main()
