#!/usr/bin/env python3
"""Investment database management CLI.

This tool upgrades a raw-fact SQLite database with the management overlay:
accounts, dictionaries, manual entries, corrections, treatment scaffolding,
and reader-friendly views. It intentionally keeps raw imported facts immutable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_MANAGEMENT_SCHEMA = WORKSPACE_ROOT / "schema" / "investment_management_schema_v1.sql"
DEFAULT_SOURCE_DB = WORKSPACE_ROOT / "exports" / "futu-ingest-2025-full" / "futu_raw_fact.sqlite"
DEFAULT_TARGET_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def apply_management_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(schema_sql)


def bootstrap_statement_accounts(conn: sqlite3.Connection, default_account_id: str = "futu_hk_main") -> int:
    if not table_exists(conn, "statement_accounts") or not table_exists(conn, "raw_statements"):
        return 0
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
          account_id, owner_label, platform, broker, account_label, base_currency, status, notes
        )
        VALUES (?, 'personal', 'futu', '富途', '富途港股主账户', 'HKD', 'active', '默认账户映射；不保存敏感账号。')
        """,
        (default_account_id,),
    )
    before = conn.execute("SELECT COUNT(*) AS c FROM statement_accounts").fetchone()["c"]
    conn.execute(
        """
        INSERT OR IGNORE INTO statement_accounts (
          import_run_id, statement_id, account_id, link_source, confidence, notes
        )
        SELECT
          import_run_id,
          statement_id,
          ?,
          'default_futu_hk_main',
          'inferred',
          '由富途 2025 结单导入批次默认挂接。'
        FROM raw_statements
        """,
        (default_account_id,),
    )
    after = conn.execute("SELECT COUNT(*) AS c FROM statement_accounts").fetchone()["c"]
    return int(after - before)


def upgrade_database(db_path: Path, schema_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        apply_management_schema(conn, schema_path)
        statement_account_links_added = bootstrap_statement_accounts(conn)
        conn.commit()
    return {
        "db_path": str(db_path),
        "management_schema": str(schema_path),
        "status": "upgraded",
        "statement_account_links_added": statement_account_links_added,
    }


def promote_database(source_db: Path, target_db: Path, schema_path: Path, replace: bool) -> dict[str, Any]:
    if not source_db.exists():
        raise FileNotFoundError(f"source db not found: {source_db}")
    if target_db.exists():
        if not replace:
            raise FileExistsError(f"target db exists; pass --replace: {target_db}")
        target_db.unlink()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if source_db.resolve() != target_db.resolve():
        shutil.copy2(source_db, target_db)
    result = upgrade_database(target_db, schema_path)
    result.update(
        {
            "source_db": str(source_db),
            "target_db": str(target_db),
            "operation": "promote",
        }
    )
    return result


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def query_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def database_status(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        tables = query_rows(
            conn,
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name",
        )
        import_runs = query_rows(
            conn,
            """
            SELECT import_run_id, created_at, status, statement_count, acceptance_status
            FROM import_runs
            ORDER BY created_at DESC
            """,
        ) if table_exists(conn, "import_runs") else []
        migrations = query_rows(
            conn,
            "SELECT migration_id, applied_at, description FROM schema_migrations ORDER BY applied_at, migration_id",
        ) if table_exists(conn, "schema_migrations") else []
        table_counts = query_rows(
            conn,
            """
            SELECT table_name, row_count
            FROM ingest_table_counts
            WHERE import_run_id = (SELECT import_run_id FROM import_runs ORDER BY created_at DESC LIMIT 1)
            ORDER BY table_name
            """,
        ) if table_exists(conn, "ingest_table_counts") and import_runs else []
        open_review_count = scalar(conn, "SELECT COUNT(*) FROM v_open_review_items") if table_exists(conn, "v_open_review_items") else None
        manual_event_count = scalar(conn, "SELECT COUNT(*) FROM manual_events") if table_exists(conn, "manual_events") else None
        correction_count = scalar(conn, "SELECT COUNT(*) FROM manual_corrections") if table_exists(conn, "manual_corrections") else None
        cash_timeline_count = scalar(conn, "SELECT COUNT(*) FROM v_cash_timeline") if table_exists(conn, "v_cash_timeline") else None
    return {
        "db_path": str(db_path),
        "status": "ok",
        "object_counts": {
            "tables": sum(1 for row in tables if row["type"] == "table"),
            "views": sum(1 for row in tables if row["type"] == "view"),
        },
        "migrations": migrations,
        "import_runs": import_runs,
        "latest_import_table_counts": table_counts,
        "management_counts": {
            "open_review_items": open_review_count,
            "manual_events": manual_event_count,
            "manual_corrections": correction_count,
            "cash_timeline_rows": cash_timeline_count,
        },
    }


def add_manual_event(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    manual_event_id = args.event_id or f"manual_event_{utc_now_compact()}"
    with connect(db_path) as conn:
        if not table_exists(conn, "manual_events"):
            raise RuntimeError("manual_events table not found; run upgrade/promote first.")
        conn.execute(
            """
            INSERT INTO manual_events (
              manual_event_id, account_id, event_date, business_type, event_subtype,
              currency, amount, description, source_label, source_ref, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manual_event_id,
                args.account_id,
                args.event_date,
                args.business_type,
                args.event_subtype,
                args.currency,
                args.amount,
                args.description,
                args.source_label,
                args.source_ref,
                args.status,
                args.notes,
            ),
        )
        conn.commit()
    return {"status": "inserted", "db_path": str(db_path), "manual_event_id": manual_event_id}


def add_correction(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    correction_id = args.correction_id or f"correction_{utc_now_compact()}"
    with connect(db_path) as conn:
        if not table_exists(conn, "manual_corrections"):
            raise RuntimeError("manual_corrections table not found; run upgrade/promote first.")
        conn.execute(
            """
            INSERT INTO manual_corrections (
              correction_id, status, target_table, target_pk, target_field,
              original_value, corrected_value, value_type, reason, reviewer,
              effective_from, effective_to, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correction_id,
                args.status,
                args.target_table,
                args.target_pk,
                args.target_field,
                args.original_value,
                args.corrected_value,
                args.value_type,
                args.reason,
                args.reviewer,
                args.effective_from,
                args.effective_to,
                args.notes,
            ),
        )
        conn.commit()
    return {"status": "inserted", "db_path": str(db_path), "correction_id": correction_id}


def write_run_record(db_path: Path, record_path: Path, payload: dict[str, Any]) -> None:
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": utc_now_iso(),
        "db_path": str(db_path),
        **payload,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="个人投资数据库管理 CLI。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    promote = subparsers.add_parser("promote", help="复制 raw SQLite，并升级为投资数据库。")
    promote.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    promote.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    promote.add_argument("--schema-path", type=Path, default=DEFAULT_MANAGEMENT_SCHEMA)
    promote.add_argument("--replace", action="store_true")
    promote.add_argument("--record-path", type=Path)

    upgrade = subparsers.add_parser("upgrade", help="在现有 SQLite 上应用管理层 schema。")
    upgrade.add_argument("--db-path", type=Path, required=True)
    upgrade.add_argument("--schema-path", type=Path, default=DEFAULT_MANAGEMENT_SCHEMA)
    upgrade.add_argument("--record-path", type=Path)

    status = subparsers.add_parser("status", help="查看数据库状态。")
    status.add_argument("--db-path", type=Path, default=DEFAULT_TARGET_DB)

    manual_event = subparsers.add_parser("add-manual-event", help="补录一个人工事件。")
    manual_event.add_argument("--db-path", type=Path, default=DEFAULT_TARGET_DB)
    manual_event.add_argument("--event-id")
    manual_event.add_argument("--account-id", default="futu_hk_main")
    manual_event.add_argument("--event-date", required=True)
    manual_event.add_argument("--business-type", required=True)
    manual_event.add_argument("--event-subtype")
    manual_event.add_argument("--currency")
    manual_event.add_argument("--amount", type=float)
    manual_event.add_argument("--description")
    manual_event.add_argument("--source-label", default="manual")
    manual_event.add_argument("--source-ref")
    manual_event.add_argument("--status", default="active")
    manual_event.add_argument("--notes")

    correction = subparsers.add_parser("add-correction", help="登记一个人工修正 overlay。")
    correction.add_argument("--db-path", type=Path, default=DEFAULT_TARGET_DB)
    correction.add_argument("--correction-id")
    correction.add_argument("--status", default="active")
    correction.add_argument("--target-table", required=True)
    correction.add_argument("--target-pk", required=True)
    correction.add_argument("--target-field", required=True)
    correction.add_argument("--original-value")
    correction.add_argument("--corrected-value", required=True)
    correction.add_argument("--value-type", default="text")
    correction.add_argument("--reason", required=True)
    correction.add_argument("--reviewer")
    correction.add_argument("--effective-from")
    correction.add_argument("--effective-to")
    correction.add_argument("--notes")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "promote":
        result = promote_database(
            source_db=args.source_db.resolve(),
            target_db=args.target_db.resolve(),
            schema_path=args.schema_path.resolve(),
            replace=args.replace,
        )
        if args.record_path:
            write_run_record(args.target_db.resolve(), args.record_path.resolve(), result)
    elif args.command == "upgrade":
        result = upgrade_database(args.db_path.resolve(), args.schema_path.resolve())
        if args.record_path:
            write_run_record(args.db_path.resolve(), args.record_path.resolve(), result)
    elif args.command == "status":
        result = database_status(args.db_path.resolve())
    elif args.command == "add-manual-event":
        result = add_manual_event(args)
    elif args.command == "add-correction":
        result = add_correction(args)
    else:
        raise ValueError(f"unknown command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
