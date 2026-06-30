#!/usr/bin/env python3
"""IPO extraction and report CLI.

This read-only report groups IPO-related cash facts, IPO allotment lots, cost
components, and downstream sale allocations. It is designed as a review surface:
raw facts stay in their source tables, while this report explains how the IPO
chain was interpreted.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "exports" / "ipo-report"

Q2 = Decimal("0.01")
IPO_CODE_RE = re.compile(r"#\s*(\d{4,5})")
HK_CODE_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")


def dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value).replace(",", ""))


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_UP)


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def dbnum(value: Decimal) -> str:
    return str(q2(value))


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone() is not None


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def latest_allocation_run_id(conn: sqlite3.Connection) -> str | None:
    if not table_exists(conn, "lot_allocation_runs"):
        return None
    row = conn.execute(
        """
        SELECT allocation_run_id
        FROM lot_allocation_runs
        WHERE status IN ('passed', 'passed_with_warnings')
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return row[0] if row else None


def normalize_date(value: Any) -> str:
    return text(value).replace("/", "-")


def ipo_code_from_description(*values: Any) -> str:
    joined = " ".join(text(value) for value in values if text(value))
    match = IPO_CODE_RE.search(joined)
    if match:
        return match.group(1).zfill(5)
    match = HK_CODE_RE.search(joined)
    return match.group(1).zfill(5) if match else ""


def source_pk_variants(row: dict[str, Any], id_col: str) -> list[str]:
    import_run_id = text(row.get("import_run_id"))
    raw_id = text(row.get(id_col))
    values = [raw_id]
    if import_run_id and raw_id:
        values.append(f"{import_run_id}:{raw_id}")
    return values


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_无数据_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(text(row.get(column)) for column in columns) + " |")
    return "\n".join(lines)


def load_ipo_cash_legs(conn: sqlite3.Connection, start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
    clauses = ["business_type = 'ipo_subscription'"]
    params: list[Any] = []
    if start_date:
        clauses.append("replace(coalesce(event_date, ''), '/', '-') >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("replace(coalesce(event_date, ''), '/', '-') <= ?")
        params.append(end_date)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT import_run_id, cash_entry_id, statement_id, period, event_date,
               cash_leg_type, currency, amount, description, source_refs
        FROM cash_ledger_entries
        WHERE {" AND ".join(clauses)}
        ORDER BY coalesce(event_date, ''), period, cash_entry_id
        """,
        tuple(params),
    )
    for row in rows:
        row["ipo_code"] = ipo_code_from_description(row.get("description"))
        row["source_pk"] = ":".join(part for part in [text(row.get("import_run_id")), text(row.get("cash_entry_id"))] if part)
    return rows


def load_ipo_asset_events(conn: sqlite3.Connection, start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
    if not table_exists(conn, "asset_movement_events"):
        return []
    clauses = ["business_type = 'ipo_subscription'"]
    params: list[Any] = []
    if start_date:
        clauses.append("replace(coalesce(event_date, ''), '/', '-') >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("replace(coalesce(event_date, ''), '/', '-') <= ?")
        params.append(end_date)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT import_run_id, asset_movement_id, statement_id, period, source_ref,
               event_date, asset_movement_type, instrument_code_raw, currency,
               quantity, amount, description_raw
        FROM asset_movement_events
        WHERE {" AND ".join(clauses)}
        ORDER BY coalesce(event_date, ''), period, asset_movement_id
        """,
        tuple(params),
    )
    for row in rows:
        row["ipo_code"] = ipo_code_from_description(row.get("description_raw"), row.get("instrument_code_raw"))
        row["source_pk"] = ":".join(part for part in [text(row.get("import_run_id")), text(row.get("asset_movement_id"))] if part)
    return rows


def load_ipo_lots(conn: sqlite3.Connection, allocation_run_id: str | None) -> list[dict[str, Any]]:
    if not allocation_run_id or not table_exists(conn, "position_lots"):
        return []
    rows = rows_as_dicts(
        conn,
        """
        SELECT allocation_run_id, lot_id, account_id, instrument_key, instrument_code,
               instrument_name, market, currency, source_type, source_table, source_pk,
               source_ref, open_date, original_quantity, remaining_quantity,
               cost_basis_total, cost_basis_principal, cost_basis_fee,
               cost_basis_status, unit_cost, notes
        FROM position_lots
        WHERE allocation_run_id = ?
          AND source_type = 'ipo_allotment'
        ORDER BY open_date, instrument_code, lot_id
        """,
        (allocation_run_id,),
    )
    component_rows = rows_as_dicts(
        conn,
        """
        SELECT lot_id, component_type, SUM(amount) AS amount
        FROM lot_cost_components
        WHERE allocation_run_id = ?
        GROUP BY lot_id, component_type
        """,
        (allocation_run_id,),
    ) if table_exists(conn, "lot_cost_components") else []
    components: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in component_rows:
        components[text(row["lot_id"])][text(row["component_type"])] = q2(dec(row["amount"]))

    allocation_rows = rows_as_dicts(
        conn,
        """
        SELECT lot_id,
               SUM(quantity_allocated) AS sold_quantity,
               SUM(proceeds_allocated) AS proceeds,
               SUM(cost_allocated) AS sold_cost,
               SUM(realized_pnl) AS realized_pnl,
               MAX(close_event_date) AS latest_close_date,
               GROUP_CONCAT(close_event_id, '; ') AS close_event_ids
        FROM lot_allocations
        WHERE allocation_run_id = ?
        GROUP BY lot_id
        """,
        (allocation_run_id,),
    ) if table_exists(conn, "lot_allocations") else []
    allocations = {text(row["lot_id"]): row for row in allocation_rows}

    for row in rows:
        lot_id = text(row["lot_id"])
        row_components = components.get(lot_id, {})
        row["ipo_code"] = text(row.get("instrument_code")).zfill(5)
        row["principal"] = dbnum(row_components.get("ipo_allotment_principal", Decimal("0")) + row_components.get("annual_ipo_net_cash_cost", Decimal("0")))
        row["application_handling_fee"] = dbnum(row_components.get("application_handling_fee", Decimal("0")))
        row["allotment_fee_or_levy"] = dbnum(row_components.get("ipo_allotment_fee_or_levy", Decimal("0")))
        allocation = allocations.get(lot_id, {})
        row["sold_quantity"] = dbnum(dec(allocation.get("sold_quantity")))
        row["sale_proceeds"] = dbnum(dec(allocation.get("proceeds")))
        row["sold_cost"] = dbnum(dec(allocation.get("sold_cost")))
        row["realized_pnl"] = dbnum(dec(allocation.get("realized_pnl")))
        row["latest_close_date"] = text(allocation.get("latest_close_date"))
        row["close_event_ids"] = text(allocation.get("close_event_ids"))
    return rows


def build_review_items(
    *,
    cash_rows: list[dict[str, Any]],
    asset_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    review_items: list[dict[str, Any]] = []
    cash_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cash_rows:
        cash_by_code[text(row.get("ipo_code"))].append(row)
    lot_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lot_rows:
        lot_by_code[text(row.get("ipo_code"))].append(row)
    asset_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in asset_rows:
        asset_by_code[text(row.get("ipo_code"))].append(row)

    for code, rows in sorted(cash_by_code.items()):
        if not code:
            review_items.append(
                {
                    "severity": "warning",
                    "check_code": "ipo_cash_code_unresolved",
                    "ipo_code": "",
                    "message": f"{len(rows)} 条 IPO 现金腿未能解析出 IPO 代码。",
                }
            )
            continue
        if code not in lot_by_code and any(dec(row.get("amount")) != 0 for row in rows):
            review_items.append(
                {
                    "severity": "info",
                    "check_code": "ipo_cash_without_allotment_lot",
                    "ipo_code": code,
                    "message": "存在 IPO 现金腿，但当前 allocation run 没有对应 IPO allotment lot；可能未中签、历史年度账单只有现金摘要，或尚未跑 allocation。",
                }
            )

    for code, lots in sorted(lot_by_code.items()):
        if code not in cash_by_code:
            review_items.append(
                {
                    "severity": "warning",
                    "check_code": "ipo_lot_without_cash_legs",
                    "ipo_code": code,
                    "message": "存在 IPO allotment lot，但未找到同代码 IPO 现金腿；需要检查年度账单/老结单是否只提供净额或 parser 是否漏抽现金。",
                }
            )
        if code not in asset_by_code:
            review_items.append(
                {
                    "severity": "warning",
                    "check_code": "ipo_lot_without_asset_event",
                    "ipo_code": code,
                    "message": "存在 IPO allotment lot，但未找到对应资产配发事件。",
                }
            )
        for lot in lots:
            if dec(lot.get("remaining_quantity")) > 0 and not text(lot.get("latest_close_date")):
                review_items.append(
                    {
                        "severity": "info",
                        "check_code": "ipo_lot_still_open",
                        "ipo_code": code,
                        "message": f"lot {lot['lot_id']} 仍有剩余数量 {lot['remaining_quantity']}，未形成完整卖出收益闭环。",
                    }
                )

    for code, rows in sorted(cash_by_code.items()):
        if not code:
            continue
        payment = abs(sum((dec(row.get("amount")) for row in rows if row.get("cash_leg_type") == "application_payment"), Decimal("0")))
        refund = sum((dec(row.get("amount")) for row in rows if row.get("cash_leg_type") == "refund"), Decimal("0"))
        handling = abs(sum((dec(row.get("amount")) for row in rows if row.get("cash_leg_type") == "application_handling_fee"), Decimal("0")))
        principal = sum((dec(lot.get("principal")) for lot in lot_by_code.get(code, [])), Decimal("0"))
        implied = q2(payment - refund - principal)
        if payment > 0 and principal > 0:
            review_items.append(
                {
                    "severity": "info",
                    "check_code": "ipo_cash_reconciliation",
                    "ipo_code": code,
                    "message": (
                        f"application_payment_abs={dbnum(payment)}, refund={dbnum(refund)}, "
                        f"allotment_principal={dbnum(principal)}, implied_diff={dbnum(implied)}, "
                        f"explicit_handling_fee={dbnum(handling)}。"
                    ),
                }
            )
    return review_items


def build_report(
    *,
    allocation_run_id: str | None,
    cash_rows: list[dict[str, Any]],
    asset_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
) -> str:
    total_cost = sum((dec(row.get("cost_basis_total")) for row in lot_rows), Decimal("0"))
    total_proceeds = sum((dec(row.get("sale_proceeds")) for row in lot_rows), Decimal("0"))
    total_pnl = sum((dec(row.get("realized_pnl")) for row in lot_rows), Decimal("0"))
    summary = [
        {"metric": "allocation_run_id", "value": allocation_run_id or ""},
        {"metric": "ipo_cash_legs", "value": str(len(cash_rows))},
        {"metric": "ipo_asset_events", "value": str(len(asset_rows))},
        {"metric": "ipo_allotment_lots", "value": str(len(lot_rows))},
        {"metric": "ipo_lot_total_cost", "value": dbnum(total_cost)},
        {"metric": "ipo_sale_proceeds", "value": dbnum(total_proceeds)},
        {"metric": "ipo_realized_pnl", "value": dbnum(total_pnl)},
        {"metric": "review_items", "value": str(len(review_items))},
    ]
    lot_columns = [
        "ipo_code",
        "instrument_name",
        "open_date",
        "original_quantity",
        "principal",
        "application_handling_fee",
        "allotment_fee_or_levy",
        "cost_basis_total",
        "remaining_quantity",
        "sale_proceeds",
        "realized_pnl",
        "cost_basis_status",
    ]
    cash_columns = ["event_date", "ipo_code", "cash_leg_type", "currency", "amount", "description"]
    review_columns = ["severity", "check_code", "ipo_code", "message"]
    return "\n".join(
        [
            "# IPO 专项报告",
            "",
            "## 摘要",
            "",
            md_table(summary, ["metric", "value"]),
            "",
            "## IPO Allotment Lots",
            "",
            md_table(lot_rows, lot_columns),
            "",
            "## IPO 现金腿",
            "",
            md_table(cash_rows, cash_columns),
            "",
            "## 复核项",
            "",
            md_table(review_items, review_columns),
            "",
        ]
    )


def run_report(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        allocation_run_id = args.allocation_run_id or latest_allocation_run_id(conn)
        cash_rows = load_ipo_cash_legs(conn, args.start_date, args.end_date)
        asset_rows = load_ipo_asset_events(conn, args.start_date, args.end_date)
        lot_rows = load_ipo_lots(conn, allocation_run_id)
        if args.start_date or args.end_date:
            start = args.start_date or "0000-00-00"
            end = args.end_date or "9999-99-99"
            lot_rows = [row for row in lot_rows if start <= normalize_date(row.get("open_date")) <= end]
        review_items = build_review_items(cash_rows=cash_rows, asset_rows=asset_rows, lot_rows=lot_rows)
        report = build_report(
            allocation_run_id=allocation_run_id,
            cash_rows=cash_rows,
            asset_rows=asset_rows,
            lot_rows=lot_rows,
            review_items=review_items,
        )

    write_csv(
        output_dir / "ipo_cash_legs.csv",
        cash_rows,
        ["event_date", "period", "ipo_code", "cash_leg_type", "currency", "amount", "description", "source_pk", "source_refs"],
    )
    write_csv(
        output_dir / "ipo_asset_events.csv",
        asset_rows,
        ["event_date", "period", "ipo_code", "asset_movement_type", "instrument_code_raw", "currency", "quantity", "amount", "description_raw", "source_pk", "source_ref"],
    )
    write_csv(
        output_dir / "ipo_lots.csv",
        lot_rows,
        [
            "lot_id",
            "ipo_code",
            "instrument_key",
            "instrument_name",
            "open_date",
            "currency",
            "original_quantity",
            "remaining_quantity",
            "principal",
            "application_handling_fee",
            "allotment_fee_or_levy",
            "cost_basis_total",
            "sale_proceeds",
            "realized_pnl",
            "cost_basis_status",
            "source_pk",
            "source_ref",
        ],
    )
    write_csv(output_dir / "ipo_review_items.csv", review_items, ["severity", "check_code", "ipo_code", "message"])
    report_path = output_dir / "ipo-report.md"
    report_path.write_text(report, encoding="utf-8")
    return {
        "status": "ok",
        "db_path": str(db_path),
        "allocation_run_id": allocation_run_id,
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "cash_legs": len(cash_rows),
        "asset_events": len(asset_rows),
        "ipo_lots": len(lot_rows),
        "review_items": len(review_items),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--allocation-run-id")
    parser.add_argument("--start-date", help="Inclusive YYYY-MM-DD filter.")
    parser.add_argument("--end-date", help="Inclusive YYYY-MM-DD filter.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_report(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
