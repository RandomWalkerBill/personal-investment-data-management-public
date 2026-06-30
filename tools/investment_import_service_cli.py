#!/usr/bin/env python3
"""Unified import-service orchestration for investment statement ingestion.

P0 intent:
- keep platform adapters and reusable rules inside the official DB;
- create candidate DBs and validation reports before any official promotion;
- make unknown platforms explicit review items instead of ad-hoc chat notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent

DEFAULT_OFFICIAL_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "import_service_schema_v1.sql"
DEFAULT_CACHE_ROOT = WORKSPACE_ROOT / "cache" / "import-service-runs"
DEFAULT_LOT_SCHEMA = WORKSPACE_ROOT / "schema" / "lot_allocation_schema_v1.sql"

FUTU_INGEST_CLI = SCRIPT_DIR / "futu_ingest_cli.py"
FUTU_ANNUAL_CLI = SCRIPT_DIR / "futu_annual_bill_ingest_cli.py"
LONGBRIDGE_INGEST_CLI = SCRIPT_DIR / "longbridge_ingest_cli.py"
XUEYING_INGEST_CLI = SCRIPT_DIR / "xueying_ingest_cli.py"
CONTINUITY_CLI = SCRIPT_DIR / "investment_continuity_check.py"
LOT_ALLOCATION_CLI = SCRIPT_DIR / "lot_allocation_cli.py"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_compact() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return slug or f"run_{utc_now_compact()}"


def json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
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


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def apply_schema(db_path: Path, schema_path: Path) -> None:
    sql = schema_path.read_text(encoding="utf-8")
    with connect(db_path) as conn:
        conn.executescript(sql)
        conn.commit()


def file_fingerprint(path: Path) -> str:
    hasher = hashlib.sha256()
    hasher.update(str(path.resolve()).encode("utf-8"))
    if path.is_file():
        hasher.update(path.name.encode("utf-8"))
        hasher.update(str(path.stat().st_size).encode("utf-8"))
        with path.open("rb") as fh:
            hasher.update(fh.read(1024 * 1024))
    elif path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            rel = child.relative_to(path)
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(str(child.stat().st_size).encode("utf-8"))
            hasher.update(str(int(child.stat().st_mtime)).encode("utf-8"))
    return hasher.hexdigest()


def list_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(p for p in input_path.rglob("*") if p.is_file())


def detect_adapter(input_path: Path, platform: str | None = None, adapter_id: str | None = None) -> dict[str, str]:
    if adapter_id:
        if adapter_id == "futu_pdf_statement_v1":
            return {"platform_id": "futu", "adapter_id": adapter_id, "reason": "explicit_adapter"}
        if adapter_id == "futu_annual_xlsx_v1":
            return {"platform_id": "futu", "adapter_id": adapter_id, "reason": "explicit_adapter"}
        if adapter_id == "longbridge_pdf_monthly_v1":
            return {"platform_id": "longbridge", "adapter_id": adapter_id, "reason": "explicit_adapter"}
        if adapter_id == "xueying_pdf_annual_activity_v1":
            return {"platform_id": "xueying", "adapter_id": adapter_id, "reason": "explicit_adapter"}
        return {"platform_id": "unknown", "adapter_id": adapter_id, "reason": "explicit_adapter_unknown"}

    files = list_input_files(input_path)
    names = [p.name for p in files]
    pdf_names = [name for name in names if name.lower().endswith(".pdf")]
    xlsx_names = [name for name in names if name.lower().endswith((".xlsx", ".xlsm", ".xls"))]

    if platform and platform != "auto":
        if platform == "futu":
            if len(xlsx_names) == 1 and not pdf_names:
                return {"platform_id": "futu", "adapter_id": "futu_annual_xlsx_v1", "reason": "explicit_platform_single_xlsx"}
            return {"platform_id": "futu", "adapter_id": "futu_pdf_statement_v1", "reason": "explicit_platform"}
        if platform == "longbridge":
            return {"platform_id": "longbridge", "adapter_id": "longbridge_pdf_monthly_v1", "reason": "explicit_platform"}
        if platform == "xueying":
            return {"platform_id": "xueying", "adapter_id": "xueying_pdf_annual_activity_v1", "reason": "explicit_platform"}
        return {"platform_id": "unknown", "adapter_id": "unknown_manual_staging", "reason": "explicit_unknown_platform"}

    futu_pdf_pattern = re.compile(r"^1001\d+-\d+-\d{6,8}-\d+\.pdf$", re.IGNORECASE)
    if pdf_names and all(futu_pdf_pattern.match(name) for name in pdf_names):
        return {"platform_id": "futu", "adapter_id": "futu_pdf_statement_v1", "reason": "futu_statement_filename_pattern"}
    if len(xlsx_names) == 1 and re.search(r"\d{4}_年度账单_\d+\.xlsx$", xlsx_names[0]):
        return {"platform_id": "futu", "adapter_id": "futu_annual_xlsx_v1", "reason": "futu_annual_bill_filename_pattern"}
    longbridge_pdf_pattern = re.compile(r"^statement-monthly-20\d{4}-H\d+\.pdf$", re.IGNORECASE)
    if pdf_names and all(longbridge_pdf_pattern.match(name) for name in pdf_names):
        return {"platform_id": "longbridge", "adapter_id": "longbridge_pdf_monthly_v1", "reason": "longbridge_statement_filename_pattern"}
    xueying_pdf_pattern = re.compile(r"^U\d+_20\d{6}_20\d{6}\.pdf$", re.IGNORECASE)
    if pdf_names and all(xueying_pdf_pattern.match(name) for name in pdf_names):
        return {"platform_id": "xueying", "adapter_id": "xueying_pdf_annual_activity_v1", "reason": "xueying_activity_statement_filename_pattern"}
    return {"platform_id": "unknown", "adapter_id": "unknown_manual_staging", "reason": "no_known_adapter_matched"}


def ensure_known_adapter(db_path: Path, schema_path: Path, adapter_id: str) -> dict[str, Any] | None:
    apply_schema(db_path, schema_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT a.adapter_id, a.platform_id, a.adapter_name, a.adapter_kind, a.source_format,
                   a.entrypoint, a.version, a.status, p.platform_name
            FROM import_service_adapters a
            JOIN import_service_platforms p ON p.platform_id = a.platform_id
            WHERE a.adapter_id = ?
            """,
            (adapter_id,),
        ).fetchone()
        return None if row is None else dict(row)


def command_to_text(command: list[str]) -> str:
    return " ".join(json.dumps(part, ensure_ascii=False) if " " in part else part for part in command)


def run_subprocess(command: list[str], cwd: Path = WORKSPACE_ROOT, env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    started_at = utc_now_iso()
    env = None
    if env_overrides:
        env = {**dict(os.environ), **env_overrides}
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
    }


def parse_json_from_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {"raw_stdout": text[-4000:]}
    return {"raw_stdout": text[-4000:]}


def classify_step_status(step_name: str, result: dict[str, Any], parsed: dict[str, Any], allow_warnings: bool) -> str:
    exit_code = result.get("exit_code")
    if exit_code == 0:
        return "passed"
    if exit_code == 2 and parsed.get("status") == "needs_review":
        if step_name == "continuity_check" and (parsed.get("failed_count") or 0) == 0 and allow_warnings:
            return "passed"
        return "needs_review"
    return "failed"


def upsert_service_run(
    db_path: Path,
    schema_path: Path,
    service_run_id: str,
    platform_id: str,
    adapter_id: str,
    input_path: Path,
    candidate_db_path: Path | None,
    official_db_path: Path,
    candidate_import_run_id: str | None,
    stage: str,
    status: str,
    promote_status: str,
    summary: dict[str, Any],
    notes: str | None = None,
) -> None:
    apply_schema(db_path, schema_path)
    payload = (
        service_run_id,
        platform_id,
        adapter_id,
        str(input_path.resolve()),
        file_fingerprint(input_path),
        None if candidate_db_path is None else str(candidate_db_path.resolve()),
        str(official_db_path.resolve()),
        candidate_import_run_id,
        stage,
        status,
        promote_status,
        utc_now_iso(),
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        notes,
    )
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO import_service_runs (
              service_run_id, platform_id, adapter_id, input_path, input_fingerprint,
              candidate_db_path, official_db_path, candidate_import_run_id,
              stage, status, promote_status, updated_at, summary_json, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_run_id) DO UPDATE SET
              platform_id = excluded.platform_id,
              adapter_id = excluded.adapter_id,
              input_path = excluded.input_path,
              input_fingerprint = excluded.input_fingerprint,
              candidate_db_path = excluded.candidate_db_path,
              official_db_path = excluded.official_db_path,
              candidate_import_run_id = excluded.candidate_import_run_id,
              stage = excluded.stage,
              status = excluded.status,
              promote_status = excluded.promote_status,
              updated_at = excluded.updated_at,
              summary_json = excluded.summary_json,
              notes = excluded.notes
            """,
            payload,
        )
        conn.commit()


def record_step(
    db_path: Path,
    service_run_id: str,
    step_order: int,
    step_name: str,
    command: list[str] | None,
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    step_id = f"{service_run_id}__{step_order:02d}_{safe_slug(step_name)}"
    summary = result or {}
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO import_service_run_steps (
              step_id, service_run_id, step_order, step_name, command, status,
              started_at, finished_at, exit_code, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(step_id) DO UPDATE SET
              command = excluded.command,
              status = excluded.status,
              finished_at = excluded.finished_at,
              exit_code = excluded.exit_code,
              summary_json = excluded.summary_json
            """,
            (
                step_id,
                service_run_id,
                step_order,
                step_name,
                None if command is None else command_to_text(command),
                status,
                summary.get("started_at", utc_now_iso()),
                summary.get("finished_at"),
                summary.get("exit_code"),
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()


def add_review_item(
    db_path: Path,
    service_run_id: str,
    severity: str,
    issue_type: str,
    message: str,
    suggested_action: str,
    source_ref: str | None = None,
) -> None:
    review_item_id = f"{service_run_id}__{safe_slug(issue_type)}__{hashlib.sha1(message.encode('utf-8')).hexdigest()[:10]}"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO import_service_review_items (
              review_item_id, service_run_id, severity, issue_type, message, suggested_action, source_ref
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (review_item_id, service_run_id, severity, issue_type, message, suggested_action, source_ref),
        )
        conn.commit()


def record_promote_decision(
    db_path: Path,
    service_run_id: str,
    decision_status: str,
    decision_reason: str,
    decided_by: str = "import_service_cli",
) -> None:
    decision_id = f"{service_run_id}__{decision_status}"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO import_service_promote_decisions (
              decision_id, service_run_id, decision_status, decided_by, decision_reason
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (decision_id, service_run_id, decision_status, decided_by, decision_reason),
        )
        conn.commit()


def sqlite_integrity(db_path: Path) -> dict[str, Any]:
    try:
        with connect(db_path) as conn:
            result = scalar(conn, "PRAGMA integrity_check")
        return {"status": "passed" if result == "ok" else "failed", "integrity_check": result}
    except Exception as exc:  # pragma: no cover - defensive path for corrupt DBs
        return {"status": "failed", "error": str(exc)}


def import_run_summary(db_path: Path, import_run_id: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"status": "missing_db"}
    with connect(db_path) as conn:
        if not table_exists(conn, "import_runs"):
            return {"status": "missing_import_runs"}
        run = conn.execute(
            """
            SELECT import_run_id, created_at, status, statement_count, acceptance_status, notes
            FROM import_runs
            WHERE import_run_id = ?
            """,
            (import_run_id,),
        ).fetchone()
        open_review_count = scalar(conn, "SELECT COUNT(*) FROM v_open_review_items") if table_exists(conn, "v_open_review_items") else None
        parser_issue_count = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM parser_issues
            WHERE import_run_id = ?
              AND (severity IN ('blocker', 'needs_review') OR status IN ('open', 'ambiguous', 'unmatched'))
            """,
            (import_run_id,),
        ) if table_exists(conn, "parser_issues") else None
        counts = rows_as_dicts(
            conn,
            """
            SELECT table_name, row_count
            FROM ingest_table_counts
            WHERE import_run_id = ?
            ORDER BY table_name
            """,
            (import_run_id,),
        ) if table_exists(conn, "ingest_table_counts") else []
    return {
        "status": "ok" if run else "missing_import_run",
        "import_run": None if run is None else dict(run),
        "open_review_count": open_review_count,
        "parser_issue_count": parser_issue_count,
        "table_counts": counts,
    }


def continuity_summary(db_path: Path, run_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        if not table_exists(conn, "continuity_check_runs"):
            return {"status": "missing_table"}
        row = conn.execute(
            """
            SELECT continuity_run_id, import_run_id, status, item_count, failed_count, warning_count
            FROM continuity_check_runs
            WHERE continuity_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return {"status": "missing_run"}
        return dict(row)


def allocation_summary(db_path: Path, run_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        if not table_exists(conn, "lot_allocation_runs"):
            return {"status": "missing_table"}
        run = conn.execute(
            """
            SELECT allocation_run_id, import_run_id, account_id, method, scope, status
            FROM lot_allocation_runs
            WHERE allocation_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            return {"status": "missing_run"}
        validation_counts = rows_as_dicts(
            conn,
            """
            SELECT status, severity, COUNT(*) AS item_count
            FROM lot_allocation_validation_items
            WHERE allocation_run_id = ?
            GROUP BY status, severity
            ORDER BY status, severity
            """,
            (run_id,),
        ) if table_exists(conn, "lot_allocation_validation_items") else []
    failed = sum(row["item_count"] for row in validation_counts if row.get("status") == "failed")
    warning = sum(
        row["item_count"]
        for row in validation_counts
        if row.get("status") not in {"passed", "skipped"} or row.get("severity") in {"warning", "review", "blocker"}
    )
    return {
        "status": "ok",
        "run": dict(run),
        "failed_count": failed,
        "warning_like_count": warning,
        "validation_counts": validation_counts,
    }


def summarize_gate(
    ingest: dict[str, Any],
    integrity: dict[str, Any],
    continuity: dict[str, Any] | None,
    allocation: dict[str, Any] | None,
    allow_warnings: bool,
) -> dict[str, Any]:
    blockers: list[str] = []
    review_items: list[str] = []

    import_run = ingest.get("import_run") or {}
    if ingest.get("status") != "ok":
        blockers.append(f"candidate import missing or invalid: {ingest.get('status')}")
    if import_run.get("acceptance_status") != "passed":
        blockers.append(f"parser acceptance_status={import_run.get('acceptance_status')}")
    if (ingest.get("open_review_count") or 0) > 0:
        review_items.append(f"open_review_items={ingest.get('open_review_count')}")
    if (ingest.get("parser_issue_count") or 0) > 0:
        review_items.append(f"parser_issue_count={ingest.get('parser_issue_count')}")
    if integrity.get("status") != "passed":
        blockers.append(f"sqlite_integrity={integrity.get('integrity_check') or integrity.get('error')}")

    if continuity is not None:
        if continuity.get("status") != "passed":
            if (continuity.get("failed_count") or 0) > 0:
                blockers.append(f"continuity_failed={continuity.get('failed_count')}")
            if (continuity.get("warning_count") or 0) > 0:
                review_items.append(f"continuity_missing_anchor={continuity.get('warning_count')}")

    if allocation is not None:
        allocation_status = (allocation.get("run") or {}).get("status")
        if allocation_status not in {None, "passed"}:
            if allocation.get("failed_count", 0) > 0:
                blockers.append(f"allocation_failed={allocation.get('failed_count')}")
            else:
                review_items.append(f"allocation_status={allocation_status}")
        if allocation.get("warning_like_count", 0) > 0:
            review_items.append(f"allocation_warning_like={allocation.get('warning_like_count')}")

    if blockers:
        status = "blocked"
        stage = "blocked"
        promote_status = "blocked"
    elif review_items and not allow_warnings:
        status = "needs_review"
        stage = "review"
        promote_status = "manual_required"
    else:
        status = "passed"
        stage = "ready_to_promote"
        promote_status = "manual_required"

    return {
        "status": status,
        "stage": stage,
        "promote_status": promote_status,
        "blockers": blockers,
        "review_items": review_items,
    }


def init_command(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    apply_schema(db_path, args.schema_path.resolve())
    with connect(db_path) as conn:
        platforms = rows_as_dicts(
            conn,
            "SELECT platform_id, platform_name, status, default_adapter_id FROM import_service_platforms ORDER BY platform_id",
        )
        adapters = rows_as_dicts(
            conn,
            "SELECT adapter_id, platform_id, adapter_kind, source_format, status FROM import_service_adapters ORDER BY adapter_id",
        )
        rule_count = scalar(conn, "SELECT COUNT(*) FROM import_service_rules")
    return {"status": "ok", "db_path": str(db_path), "platforms": platforms, "adapters": adapters, "rule_count": rule_count}


def adapters_command(args: argparse.Namespace) -> dict[str, Any]:
    apply_schema(args.db_path.resolve(), args.schema_path.resolve())
    with connect(args.db_path.resolve()) as conn:
        adapters = rows_as_dicts(
            conn,
            """
            SELECT a.adapter_id, a.platform_id, p.platform_name, a.adapter_name,
                   a.adapter_kind, a.source_format, a.entrypoint, a.version, a.status
            FROM import_service_adapters a
            JOIN import_service_platforms p ON p.platform_id = a.platform_id
            ORDER BY a.platform_id, a.adapter_id
            """,
        )
    return {"status": "ok", "db_path": str(args.db_path.resolve()), "adapters": adapters}


def rules_command(args: argparse.Namespace) -> dict[str, Any]:
    apply_schema(args.db_path.resolve(), args.schema_path.resolve())
    filters = []
    params: list[Any] = []
    if args.platform_id:
        filters.append("platform_id = ?")
        params.append(args.platform_id)
    if args.adapter_id:
        filters.append("adapter_id = ?")
        params.append(args.adapter_id)
    if args.status:
        filters.append("status = ?")
        params.append(args.status)
    where = "" if not filters else "WHERE " + " AND ".join(filters)
    with connect(args.db_path.resolve()) as conn:
        rules = rows_as_dicts(
            conn,
            f"""
            SELECT rule_id, platform_id, adapter_id, rule_scope, rule_key,
                   rule_title, confidence, status, canonical_action
            FROM import_service_rules
            {where}
            ORDER BY platform_id, adapter_id, rule_scope, rule_key
            """,
            tuple(params),
        )
    return {"status": "ok", "db_path": str(args.db_path.resolve()), "rules": rules}


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    input_path = args.input_path.resolve()
    official_db = args.db_path.resolve()
    service_run_id = args.service_run_id or f"import_service_{utc_now_compact()}"
    service_dir = (args.work_dir or DEFAULT_CACHE_ROOT / service_run_id).resolve()
    candidate_db = (args.candidate_db or service_dir / "candidate.sqlite").resolve()
    detection = detect_adapter(input_path, args.platform, args.adapter_id)
    adapter = ensure_known_adapter(official_db, args.schema_path.resolve(), detection["adapter_id"])
    file_count = len(list_input_files(input_path))
    candidate_import_run_id = args.candidate_import_run_id or f"{safe_slug(service_run_id)}_candidate"
    plan = {
        "service_run_id": service_run_id,
        "platform_id": detection["platform_id"],
        "adapter_id": detection["adapter_id"],
        "adapter_status": None if adapter is None else adapter["status"],
        "detection_reason": detection["reason"],
        "input_path": str(input_path),
        "input_fingerprint": file_fingerprint(input_path),
        "file_count": file_count,
        "candidate_db_path": str(candidate_db),
        "official_db_path": str(official_db),
        "candidate_import_run_id": candidate_import_run_id,
        "work_dir": str(service_dir),
        "commands": planned_commands(
            input_path=input_path,
            adapter_id=detection["adapter_id"],
            service_dir=service_dir,
            candidate_db=candidate_db,
            candidate_import_run_id=candidate_import_run_id,
            service_run_id=service_run_id,
            password_env=args.pdf_password_env,
        ),
    }
    upsert_service_run(
        db_path=official_db,
        schema_path=args.schema_path.resolve(),
        service_run_id=service_run_id,
        platform_id=detection["platform_id"],
        adapter_id=detection["adapter_id"],
        input_path=input_path,
        candidate_db_path=candidate_db,
        official_db_path=official_db,
        candidate_import_run_id=candidate_import_run_id,
        stage="planned",
        status="needs_review" if detection["platform_id"] == "unknown" else "passed",
        promote_status="manual_required" if detection["platform_id"] == "unknown" else "not_requested",
        summary=plan,
        notes="plan only",
    )
    if detection["platform_id"] == "unknown":
        add_review_item(
            official_db,
            service_run_id,
            "blocker",
            "unknown_platform_adapter",
            "没有匹配到已知平台适配器，不能自动导入。",
            "先确认结单字段、业务类型映射、现金/持仓口径，再登记 active adapter。",
            str(input_path),
        )
        record_promote_decision(official_db, service_run_id, "blocked", "未知平台不得自动 promote。")
    return {"status": "planned", "plan": plan}


def planned_commands(
    input_path: Path,
    adapter_id: str,
    service_dir: Path,
    candidate_db: Path,
    candidate_import_run_id: str,
    service_run_id: str,
    password_env: str = "LONGBRIDGE_PDF_PASSWORD",
) -> list[dict[str, Any]]:
    continuity_run_id = f"{safe_slug(service_run_id)}_continuity"
    allocation_run_id = f"{safe_slug(service_run_id)}_allocation"
    if adapter_id == "futu_pdf_statement_v1":
        ingest_command = [
            sys.executable,
            str(FUTU_INGEST_CLI),
            "--pdf-dir",
            str(input_path),
            "--work-dir",
            str(service_dir / "futu-ingest"),
            "--run-id",
            candidate_import_run_id,
            "--db-path",
            str(candidate_db),
            "--review-xlsx",
            str(service_dir / "review" / "futu-review.xlsx"),
            "--replace-db",
            "--strict",
        ]
    elif adapter_id == "futu_annual_xlsx_v1":
        ingest_command = [
            sys.executable,
            str(FUTU_ANNUAL_CLI),
            "--xlsx",
            str(input_path),
            "--work-dir",
            str(service_dir / "futu-annual-ingest"),
            "--run-id",
            candidate_import_run_id,
            "--db-path",
            str(candidate_db),
            "--replace-db",
            "--strict",
        ]
    elif adapter_id == "longbridge_pdf_monthly_v1":
        ingest_command = [
            sys.executable,
            str(LONGBRIDGE_INGEST_CLI),
            "--pdf-dir",
            str(input_path),
            "--work-dir",
            str(service_dir / "longbridge-ingest"),
            "--run-id",
            candidate_import_run_id,
            "--db-path",
            str(candidate_db),
            "--password-env",
            password_env,
            "--replace-db",
            "--strict",
        ]
    elif adapter_id == "xueying_pdf_annual_activity_v1":
        ingest_command = [
            sys.executable,
            str(XUEYING_INGEST_CLI),
            "--pdf-dir",
            str(input_path),
            "--work-dir",
            str(service_dir / "xueying-ingest"),
            "--run-id",
            candidate_import_run_id,
            "--db-path",
            str(candidate_db),
            "--replace-db",
            "--strict",
        ]
    else:
        return []

    continuity_command = [
        sys.executable,
        str(CONTINUITY_CLI),
        "--db-path",
        str(candidate_db),
        "--import-run-id",
        candidate_import_run_id,
        "--run-id",
        continuity_run_id,
        "--report-md",
        str(service_dir / "reports" / "continuity-report.md"),
    ]
    if adapter_id in {"longbridge_pdf_monthly_v1", "xueying_pdf_annual_activity_v1"}:
        return [
            {"step": "candidate_import", "command": ingest_command},
            {"step": "continuity_check", "command": continuity_command},
        ]

    allocation_command = [
        sys.executable,
        str(LOT_ALLOCATION_CLI),
        "run",
        "--db-path",
        str(candidate_db),
        "--schema-path",
        str(DEFAULT_LOT_SCHEMA),
        "--report-path",
        str(service_dir / "reports" / "lot-allocation-report.md"),
        "--export-dir",
        str(service_dir / "lot-allocation"),
        "--run-id",
        allocation_run_id,
        "--import-run-id",
        candidate_import_run_id,
        "--account-id",
        "futu_hk_main",
        "--replace",
    ]
    return [
        {"step": "candidate_import", "command": ingest_command},
        {"step": "continuity_check", "command": continuity_command},
        {"step": "lot_allocation_check", "command": allocation_command},
    ]


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    input_path = args.input_path.resolve()
    official_db = args.db_path.resolve()
    service_run_id = args.service_run_id or f"import_service_{utc_now_compact()}"
    service_dir = (args.work_dir or DEFAULT_CACHE_ROOT / service_run_id).resolve()
    candidate_db = (args.candidate_db or service_dir / "candidate.sqlite").resolve()
    candidate_import_run_id = args.candidate_import_run_id or f"{safe_slug(service_run_id)}_candidate"
    detection = detect_adapter(input_path, args.platform, args.adapter_id)
    adapter = ensure_known_adapter(official_db, args.schema_path.resolve(), detection["adapter_id"])
    service_dir.mkdir(parents=True, exist_ok=True)

    base_summary = {
        "service_run_id": service_run_id,
        "platform_id": detection["platform_id"],
        "adapter_id": detection["adapter_id"],
        "adapter_status": None if adapter is None else adapter["status"],
        "detection_reason": detection["reason"],
        "input_path": str(input_path),
        "candidate_db_path": str(candidate_db),
        "candidate_import_run_id": candidate_import_run_id,
        "work_dir": str(service_dir),
    }
    upsert_service_run(
        official_db,
        args.schema_path.resolve(),
        service_run_id,
        detection["platform_id"],
        detection["adapter_id"],
        input_path,
        candidate_db,
        official_db,
        candidate_import_run_id,
        "candidate_import",
        "running",
        "not_requested",
        base_summary,
    )

    if adapter is None or adapter.get("status") != "active" or detection["platform_id"] == "unknown":
        message = "没有可用 active adapter，已停止在人工复核。"
        add_review_item(official_db, service_run_id, "blocker", "adapter_not_active", message, "先建立并验证平台 parser/字段映射规则。", str(input_path))
        record_promote_decision(official_db, service_run_id, "blocked", message)
        result = {**base_summary, "status": "blocked", "blockers": [message]}
        upsert_service_run(
            official_db,
            args.schema_path.resolve(),
            service_run_id,
            detection["platform_id"],
            detection["adapter_id"],
            input_path,
            candidate_db,
            official_db,
            candidate_import_run_id,
            "blocked",
            "blocked",
            "blocked",
            result,
        )
        return result

    commands = planned_commands(
        input_path,
        detection["adapter_id"],
        service_dir,
        candidate_db,
        candidate_import_run_id,
        service_run_id,
        args.pdf_password_env,
    )
    if args.dry_run:
        result = {**base_summary, "status": "dry_run", "commands": commands}
        record_step(official_db, service_run_id, 1, "dry_run_plan", None, "skipped", result)
        upsert_service_run(
            official_db,
            args.schema_path.resolve(),
            service_run_id,
            detection["platform_id"],
            detection["adapter_id"],
            input_path,
            candidate_db,
            official_db,
            candidate_import_run_id,
            "planned",
            "passed",
            "not_requested",
            result,
            "dry run only; no candidate DB generated",
        )
        return result

    step_results: list[dict[str, Any]] = []
    for index, command_info in enumerate(commands, start=1):
        command = command_info["command"]
        step_name = command_info["step"]
        env_overrides = None
        if detection["adapter_id"] == "longbridge_pdf_monthly_v1" and step_name == "candidate_import":
            if args.pdf_password:
                env_overrides = {args.pdf_password_env: args.pdf_password}
            elif args.pdf_password_env and args.pdf_password_env not in os.environ:
                add_review_item(
                    official_db,
                    service_run_id,
                    "blocker",
                    "missing_pdf_password_env",
                    f"长桥 PDF 需要通过环境变量 {args.pdf_password_env} 提供密码。",
                    "重新运行时传入 --pdf-password 或先设置对应环境变量。",
                    str(input_path),
                )
                result = {
                    "started_at": utc_now_iso(),
                    "finished_at": utc_now_iso(),
                    "exit_code": 2,
                    "stdout": "",
                    "stderr": f"missing password env: {args.pdf_password_env}",
                }
                record_step(official_db, service_run_id, index, step_name, command, "failed", result)
                step_results.append({"step": step_name, "status": "failed", "result": result})
                if args.stop_on_failure:
                    break
                continue
        result = run_subprocess(command, env_overrides=env_overrides)
        parsed = parse_json_from_stdout(result.get("stdout", ""))
        result["parsed_stdout"] = parsed
        step_status = classify_step_status(step_name, result, parsed, args.allow_warnings)
        record_step(official_db, service_run_id, index, step_name, command, step_status, result)
        step_results.append({"step": step_name, "status": step_status, "result": result})
        if step_status == "failed" and args.stop_on_failure:
            break

    integrity = sqlite_integrity(candidate_db)
    record_step(official_db, service_run_id, 90, "sqlite_integrity_check", None, integrity["status"], integrity)
    ingest = import_run_summary(candidate_db, candidate_import_run_id)

    continuity_run_id = f"{safe_slug(service_run_id)}_continuity"
    allocation_run_id = f"{safe_slug(service_run_id)}_allocation"
    continuity = continuity_summary(candidate_db, continuity_run_id) if candidate_db.exists() else None
    allocation = allocation_summary(candidate_db, allocation_run_id) if candidate_db.exists() else None
    gate = summarize_gate(ingest, integrity, continuity, allocation, args.allow_warnings)

    summary = {
        **base_summary,
        "status": gate["status"],
        "stage": gate["stage"],
        "promote_status": gate["promote_status"],
        "step_results": step_results,
        "ingest": ingest,
        "sqlite_integrity": integrity,
        "continuity": continuity,
        "allocation": allocation,
        "blockers": gate["blockers"],
        "review_items": gate["review_items"],
        "reports": {
            "service_dir": str(service_dir),
            "continuity_report": str(service_dir / "reports" / "continuity-report.md"),
            "lot_allocation_report": str(service_dir / "reports" / "lot-allocation-report.md"),
        },
    }
    if gate["blockers"]:
        for blocker in gate["blockers"]:
            add_review_item(official_db, service_run_id, "blocker", "gate_blocker", blocker, "修复 parser/数据/模型后重新生成候选库。")
    for item in gate["review_items"]:
        add_review_item(official_db, service_run_id, "needs_review", "gate_needs_review", item, "人工确认是否属于白名单或需要新增规则。")

    record_promote_decision(
        official_db,
        service_run_id,
        "blocked" if gate["status"] == "blocked" else "manual_required",
        "P0 导入服务默认不自动 promote；候选库通过后仍需人工确认并进入后续增量 promote 设计。",
    )
    upsert_service_run(
        official_db,
        args.schema_path.resolve(),
        service_run_id,
        detection["platform_id"],
        detection["adapter_id"],
        input_path,
        candidate_db,
        official_db,
        candidate_import_run_id,
        gate["stage"],
        gate["status"],
        gate["promote_status"],
        summary,
    )
    return summary


def record_rule_command(args: argparse.Namespace) -> dict[str, Any]:
    apply_schema(args.db_path.resolve(), args.schema_path.resolve())
    rule_id = args.rule_id or f"rule_{safe_slug(args.platform_id or 'global')}_{safe_slug(args.rule_scope)}_{safe_slug(args.rule_key)}"
    with connect(args.db_path.resolve()) as conn:
        conn.execute(
            """
            INSERT INTO import_service_rules (
              rule_id, platform_id, adapter_id, rule_scope, rule_key, rule_title, rule_body,
              trigger_pattern, canonical_action, confidence, status, created_from_run_id, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform_id, adapter_id, rule_scope, rule_key) DO UPDATE SET
              rule_title = excluded.rule_title,
              rule_body = excluded.rule_body,
              trigger_pattern = excluded.trigger_pattern,
              canonical_action = excluded.canonical_action,
              confidence = excluded.confidence,
              status = excluded.status,
              updated_at = datetime('now'),
              notes = excluded.notes
            """,
            (
                rule_id,
                args.platform_id,
                args.adapter_id,
                args.rule_scope,
                args.rule_key,
                args.rule_title,
                args.rule_body,
                args.trigger_pattern,
                args.canonical_action,
                args.confidence,
                args.status,
                args.created_from_run_id,
                args.notes,
            ),
        )
        conn.commit()
    return {"status": "upserted", "db_path": str(args.db_path.resolve()), "rule_id": rule_id}


def status_command(args: argparse.Namespace) -> dict[str, Any]:
    apply_schema(args.db_path.resolve(), args.schema_path.resolve())
    with connect(args.db_path.resolve()) as conn:
        runs = rows_as_dicts(
            conn,
            """
            SELECT service_run_id, platform_id, adapter_id, stage, status, promote_status,
                   candidate_import_run_id, created_at, updated_at
            FROM import_service_runs
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        )
        review_items = rows_as_dicts(
            conn,
            """
            SELECT severity, status, issue_type, COUNT(*) AS item_count
            FROM import_service_review_items
            GROUP BY severity, status, issue_type
            ORDER BY severity, status, issue_type
            """,
        )
    return {"status": "ok", "db_path": str(args.db_path.resolve()), "runs": runs, "review_items": review_items}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统一投资结单导入服务 CLI。")
    parser.add_argument("--schema-path", type=Path, default=DEFAULT_SCHEMA)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="初始化导入服务 schema / seed。")
    init.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    init.set_defaults(func=init_command)

    adapters = subparsers.add_parser("adapters", help="列出已登记平台适配器。")
    adapters.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    adapters.set_defaults(func=adapters_command)

    rules = subparsers.add_parser("rules", help="列出规则经验库。")
    rules.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    rules.add_argument("--platform-id")
    rules.add_argument("--adapter-id")
    rules.add_argument("--status", default="active")
    rules.set_defaults(func=rules_command)

    status = subparsers.add_parser("status", help="查看最近导入服务运行和复核项。")
    status.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=status_command)

    plan = subparsers.add_parser("plan", help="识别输入并生成候选导入计划，不写 candidate DB。")
    plan.add_argument("--input-path", type=Path, required=True)
    plan.add_argument("--platform", default="auto")
    plan.add_argument("--adapter-id")
    plan.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    plan.add_argument("--candidate-db", type=Path)
    plan.add_argument("--candidate-import-run-id")
    plan.add_argument("--service-run-id")
    plan.add_argument("--work-dir", type=Path)
    plan.add_argument("--pdf-password-env", default="LONGBRIDGE_PDF_PASSWORD", help="长桥等加密 PDF adapter 使用的密码环境变量名；计划中不记录明文密码。")
    plan.set_defaults(func=build_plan)

    run = subparsers.add_parser("run", help="运行候选导入和校验 gate；P0 不自动 promote。")
    run.add_argument("--input-path", type=Path, required=True)
    run.add_argument("--platform", default="auto")
    run.add_argument("--adapter-id")
    run.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    run.add_argument("--candidate-db", type=Path)
    run.add_argument("--candidate-import-run-id")
    run.add_argument("--service-run-id")
    run.add_argument("--work-dir", type=Path)
    run.add_argument("--pdf-password", help="加密 PDF 密码；仅注入子进程环境，不写入命令记录。")
    run.add_argument("--pdf-password-env", default="LONGBRIDGE_PDF_PASSWORD", help="加密 PDF 密码环境变量名。")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-warnings", action="store_true", help="允许已知 warning 进入 ready_to_promote；仍不会自动 promote。")
    run.add_argument("--stop-on-failure", action=argparse.BooleanOptionalAction, default=True)
    run.set_defaults(func=run_command)

    record_rule = subparsers.add_parser("record-rule", help="登记或更新一条处理经验/规则。")
    record_rule.add_argument("--db-path", type=Path, default=DEFAULT_OFFICIAL_DB)
    record_rule.add_argument("--rule-id")
    record_rule.add_argument("--platform-id")
    record_rule.add_argument("--adapter-id")
    record_rule.add_argument("--rule-scope", required=True, choices=["parser", "mapping", "validation", "normalization", "treatment", "manual_review", "promotion"])
    record_rule.add_argument("--rule-key", required=True)
    record_rule.add_argument("--rule-title", required=True)
    record_rule.add_argument("--rule-body", required=True)
    record_rule.add_argument("--trigger-pattern")
    record_rule.add_argument("--canonical-action")
    record_rule.add_argument("--confidence", default="reviewed", choices=["confirmed", "reviewed", "inferred", "draft"])
    record_rule.add_argument("--status", default="active", choices=["active", "draft", "deprecated"])
    record_rule.add_argument("--created-from-run-id")
    record_rule.add_argument("--notes")
    record_rule.set_defaults(func=record_rule_command)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 1
    print_json(result)
    if result.get("status") == "failed":
        return 1
    if args.command == "run" and result.get("status") in {"blocked", "needs_review"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
