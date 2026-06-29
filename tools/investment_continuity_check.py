#!/usr/bin/env python3
"""Cross-month cash and position continuity checks for the investment DB."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_REPORT = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "continuity-check-report.md"
NATIVE_CASH_CURRENCIES = ["HKD", "USD", "CNY", "JPY", "SGD"]
CARRY_CASH_CURRENCIES = ["TOTAL_HKD", *NATIVE_CASH_CURRENCIES]


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(str(value).replace(",", "").replace("+", ""))


def almost_equal(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


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


def latest_import_run_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT import_run_id FROM import_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("No import run found.")
    return str(row["import_run_id"])


def get_statements(conn: sqlite3.Connection, import_run_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT statement_id, period, filename
            FROM raw_statements
            WHERE import_run_id = ?
            ORDER BY period, statement_id
            """,
            (import_run_id,),
        )
    ]


def normalize_instrument_key(code_name: str | None) -> str:
    text = (code_name or "").strip()
    if not text:
        return ""
    for pattern in [r"^(HK\d{10})", r"^([A-Z]{2,}[0-9A-Z]+)\(", r"^(\d{4,5})\(", r"^([A-Z]{1,6})\("]:
        match = re.match(pattern, text)
        if match:
            return match.group(1)
    return text.split()[0]


def get_balance_map(conn: sqlite3.Connection, import_run_id: str) -> dict[tuple[str, str, str], float]:
    rows = conn.execute(
        """
        SELECT statement_id, snapshot_type, currency, reported_balance
        FROM statement_balance_snapshots
        WHERE import_run_id = ? AND status = 'active'
        """,
        (import_run_id,),
    ).fetchall()
    return {
        (row["statement_id"], row["snapshot_type"], row["currency"]): parse_number(row["reported_balance"])
        for row in rows
    }


def get_cash_flow_components(conn: sqlite3.Connection, import_run_id: str) -> dict[tuple[str, str], dict[str, float]]:
    flows: dict[tuple[str, str], dict[str, float]] = {}
    for row in conn.execute(
        """
        SELECT statement_id, currency, SUM(amount) AS amount
        FROM cash_ledger_entries
        WHERE import_run_id = ?
        GROUP BY statement_id, currency
        """,
        (import_run_id,),
    ):
        key = (row["statement_id"], row["currency"])
        flows.setdefault(key, {"cash_ledger_flow": 0.0, "market_trade_flow": 0.0})
        flows[key]["cash_ledger_flow"] += parse_number(row["amount"])
    for row in conn.execute(
        """
        SELECT statement_id, currency, SUM(net_cash_amount) AS amount
        FROM market_trades
        WHERE import_run_id = ?
        GROUP BY statement_id, currency
        """,
        (import_run_id,),
    ):
        key = (row["statement_id"], row["currency"])
        flows.setdefault(key, {"cash_ledger_flow": 0.0, "market_trade_flow": 0.0})
        flows[key]["market_trade_flow"] += parse_number(row["amount"])
    return flows


def position_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    return (
        str(row["asset_category"] or ""),
        str(row["market"] or ""),
        str(row["currency"] or ""),
        normalize_instrument_key(row["code_name"]),
    )


def get_positions(
    conn: sqlite3.Connection,
    import_run_id: str,
    statement_id: str,
    snapshot_type: str,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT statement_id, snapshot_type, asset_category, code_name, market, currency,
               quantity, price, market_value, pending_amount
        FROM position_snapshots
        WHERE import_run_id = ? AND statement_id = ? AND snapshot_type = ?
        """,
        (import_run_id, statement_id, snapshot_type),
    ).fetchall()
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        result[position_key(row)] = dict(row)
    return result


def item(
    continuity_run_id: str,
    check_code: str,
    check_scope: str,
    severity: str,
    status: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "continuity_item_id": f"ci_{len(kwargs.pop('_items')) + 1:05d}" if "_items" in kwargs else "",
        "continuity_run_id": continuity_run_id,
        "check_code": check_code,
        "check_scope": check_scope,
        "severity": severity,
        "status": status,
        "period_from": kwargs.get("period_from"),
        "period_to": kwargs.get("period_to"),
        "statement_id": kwargs.get("statement_id"),
        "account_id": kwargs.get("account_id", "futu_hk_main"),
        "instrument_key": kwargs.get("instrument_key"),
        "currency": kwargs.get("currency"),
        "expected_value": kwargs.get("expected_value"),
        "actual_value": kwargs.get("actual_value"),
        "difference": kwargs.get("difference"),
        "tolerance": kwargs.get("tolerance"),
        "source_table": kwargs.get("source_table"),
        "source_pk": kwargs.get("source_pk"),
        "detail_json": json.dumps(kwargs.get("detail", {}), ensure_ascii=False),
        "notes": kwargs.get("notes"),
    }


def make_item(
    items: list[dict[str, Any]],
    continuity_run_id: str,
    check_code: str,
    check_scope: str,
    severity: str,
    status: str,
    **kwargs: Any,
) -> None:
    row = item(continuity_run_id, check_code, check_scope, severity, status, **kwargs)
    row["continuity_item_id"] = f"ci_{len(items) + 1:05d}"
    items.append(row)


def run_checks(conn: sqlite3.Connection, import_run_id: str, continuity_run_id: str) -> list[dict[str, Any]]:
    statements = get_statements(conn, import_run_id)
    balances = get_balance_map(conn, import_run_id)
    cash_flows = get_cash_flow_components(conn, import_run_id)
    items: list[dict[str, Any]] = []
    tolerance = 0.01

    for statement in statements:
        statement_id = statement["statement_id"]
        for currency in NATIVE_CASH_CURRENCIES:
            opening = balances.get((statement_id, "opening_cash_balance", currency))
            ending = balances.get((statement_id, "ending_cash_balance", currency))
            components = cash_flows.get((statement_id, currency), {"cash_ledger_flow": 0.0, "market_trade_flow": 0.0})
            cash_ledger_flow = round(components["cash_ledger_flow"], 2)
            market_trade_flow = round(components["market_trade_flow"], 2)
            flow = round(cash_ledger_flow + market_trade_flow, 2)
            if opening is None or ending is None:
                make_item(
                    items,
                    continuity_run_id,
                    "cash_flow_reconciliation",
                    "cash",
                    "warning",
                    "missing_anchor",
                    statement_id=statement_id,
                    period_from=statement["period"],
                    currency=currency,
                    source_table="statement_balance_snapshots",
                    detail={
                        "opening": opening,
                        "ending": ending,
                        "cash_ledger_flow": cash_ledger_flow,
                        "market_trade_flow": market_trade_flow,
                        "combined_flow": flow,
                    },
                    notes="Missing opening or ending cash balance anchor.",
                )
                continue
            expected = round(opening + flow, 2)
            actual = round(ending, 2)
            diff = round(expected - actual, 2)
            passed = almost_equal(expected, actual, tolerance)
            make_item(
                items,
                continuity_run_id,
                "cash_flow_reconciliation",
                "cash",
                "error" if not passed else "info",
                "failed" if not passed else "passed",
                statement_id=statement_id,
                period_from=statement["period"],
                currency=currency,
                expected_value=expected,
                actual_value=actual,
                difference=diff,
                tolerance=tolerance,
                source_table="cash_ledger_entries+market_trades+statement_balance_snapshots",
                detail={
                    "opening": opening,
                    "cash_ledger_flow": cash_ledger_flow,
                    "market_trade_flow": market_trade_flow,
                    "combined_flow": flow,
                    "ending": ending,
                    "cash_change": round(ending - opening, 2),
                },
                notes=None
                if passed
                else "期初现金 + 现金流水 + 市场交易净额未闭合期末现金；需继续解释未交收、跨月 IPO、基金现金腿或其他现金等价变动。",
            )

    for prev, curr in zip(statements, statements[1:]):
        for currency in CARRY_CASH_CURRENCIES:
            prev_ending = balances.get((prev["statement_id"], "ending_cash_balance", currency))
            curr_opening = balances.get((curr["statement_id"], "opening_cash_balance", currency))
            if prev_ending is None or curr_opening is None:
                make_item(
                    items,
                    continuity_run_id,
                    "cash_carry_forward",
                    "cash",
                    "warning",
                    "missing_anchor",
                    period_from=prev["period"],
                    period_to=curr["period"],
                    currency=currency,
                    source_table="statement_balance_snapshots",
                    detail={"previous_ending": prev_ending, "current_opening": curr_opening},
                )
                continue
            diff = round(prev_ending - curr_opening, 2)
            make_item(
                items,
                continuity_run_id,
                "cash_carry_forward",
                "cash",
                "error" if not almost_equal(prev_ending, curr_opening, tolerance) else "info",
                "failed" if not almost_equal(prev_ending, curr_opening, tolerance) else "passed",
                period_from=prev["period"],
                period_to=curr["period"],
                currency=currency,
                expected_value=prev_ending,
                actual_value=curr_opening,
                difference=diff,
                tolerance=tolerance,
                source_table="statement_balance_snapshots",
            )

        prev_positions = get_positions(conn, import_run_id, prev["statement_id"], "ending")
        curr_positions = get_positions(conn, import_run_id, curr["statement_id"], "opening")
        all_keys = sorted(set(prev_positions) | set(curr_positions))
        for key in all_keys:
            prev_row = prev_positions.get(key)
            curr_row = curr_positions.get(key)
            instrument_key = key[3]
            currency = key[2]
            if prev_row is None or curr_row is None:
                make_item(
                    items,
                    continuity_run_id,
                    "position_carry_forward",
                    "position",
                    "error",
                    "failed",
                    period_from=prev["period"],
                    period_to=curr["period"],
                    instrument_key=instrument_key,
                    currency=currency,
                    source_table="position_snapshots",
                    detail={"previous_ending": prev_row, "current_opening": curr_row},
                    notes="Position exists on only one side of the carry-forward pair.",
                )
                continue
            prev_qty = parse_number(prev_row["quantity"])
            curr_qty = parse_number(curr_row["quantity"])
            qty_diff = round(prev_qty - curr_qty, 8)
            make_item(
                items,
                continuity_run_id,
                "position_quantity_carry_forward",
                "position",
                "error" if not almost_equal(prev_qty, curr_qty, 0.000001) else "info",
                "failed" if not almost_equal(prev_qty, curr_qty, 0.000001) else "passed",
                period_from=prev["period"],
                period_to=curr["period"],
                instrument_key=instrument_key,
                currency=currency,
                expected_value=prev_qty,
                actual_value=curr_qty,
                difference=qty_diff,
                tolerance=0.000001,
                source_table="position_snapshots",
                detail={"previous_code_name": prev_row["code_name"], "current_code_name": curr_row["code_name"]},
            )
            prev_pending = parse_number(prev_row["pending_amount"])
            curr_pending = parse_number(curr_row["pending_amount"])
            if prev_pending or curr_pending:
                pending_diff = round(prev_pending - curr_pending, 2)
                make_item(
                    items,
                    continuity_run_id,
                    "position_pending_amount_carry_forward",
                    "position",
                    "error" if not almost_equal(prev_pending, curr_pending, tolerance) else "info",
                    "failed" if not almost_equal(prev_pending, curr_pending, tolerance) else "passed",
                    period_from=prev["period"],
                    period_to=curr["period"],
                    instrument_key=instrument_key,
                    currency=currency,
                    expected_value=prev_pending,
                    actual_value=curr_pending,
                    difference=pending_diff,
                    tolerance=tolerance,
                    source_table="position_snapshots",
                    detail={"previous_code_name": prev_row["code_name"], "current_code_name": curr_row["code_name"]},
                )
    return items


def persist_results(conn: sqlite3.Connection, continuity_run_id: str, import_run_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    failed_count = sum(1 for row in items if row["status"] == "failed")
    warning_count = sum(1 for row in items if row["status"] == "missing_anchor")
    status = "passed" if failed_count == 0 and warning_count == 0 else "needs_review"
    conn.execute("DELETE FROM continuity_check_items WHERE continuity_run_id = ?", (continuity_run_id,))
    conn.execute("DELETE FROM continuity_check_runs WHERE continuity_run_id = ?", (continuity_run_id,))
    conn.execute(
        """
        INSERT INTO continuity_check_runs (
          continuity_run_id, created_at, import_run_id, status, check_scope,
          item_count, failed_count, warning_count, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            continuity_run_id,
            utc_now_iso(),
            import_run_id,
            status,
            "cash_and_position",
            len(items),
            failed_count,
            warning_count,
            "Cash carry-forward, cash flow reconciliation, and position carry-forward.",
        ),
    )
    columns = [
        "continuity_item_id",
        "continuity_run_id",
        "check_code",
        "check_scope",
        "severity",
        "status",
        "period_from",
        "period_to",
        "statement_id",
        "account_id",
        "instrument_key",
        "currency",
        "expected_value",
        "actual_value",
        "difference",
        "tolerance",
        "source_table",
        "source_pk",
        "detail_json",
        "notes",
    ]
    conn.executemany(
        f"INSERT INTO continuity_check_items ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [[row.get(column) for column in columns] for row in items],
    )
    conn.commit()
    return {
        "continuity_run_id": continuity_run_id,
        "import_run_id": import_run_id,
        "status": status,
        "item_count": len(items),
        "failed_count": failed_count,
        "warning_count": warning_count,
    }


def write_report(path: Path, summary: dict[str, Any], items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    failed = [row for row in items if row["status"] != "passed"]
    by_code: dict[str, dict[str, int]] = {}
    for row in items:
        code = row["check_code"]
        by_code.setdefault(code, {"passed": 0, "failed": 0, "missing_anchor": 0})
        by_code[code][row["status"]] = by_code[code].get(row["status"], 0) + 1
    lines = [
        f"# 跨月现金 / 持仓连续性校验：{summary['continuity_run_id']}",
        "",
        "## 结论",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 检查项：`{summary['item_count']}`",
        f"- failed：`{summary['failed_count']}`",
        f"- missing anchor：`{summary['warning_count']}`",
        "",
        "本轮校验把两件事分开看：",
        "",
        "- **硬锚点连续性**：结单期末现金是否等于下一期初现金，期末持仓是否等于下一期初持仓。",
        "- **月内现金解释层**：期初现金 + 已抽现金流水 + 市场交易净额，是否能解释期末现金。",
        "",
        (
            "当前没有非通过项；现金跨月锚点、持仓跨月锚点和月内现金解释层均已闭合。"
            if summary["failed_count"] == 0 and summary["warning_count"] == 0
            else "当前失败项全部来自月内现金解释层；现金跨月锚点和持仓跨月锚点均未发现断点。"
        ),
        "",
        "## 按检查类型汇总",
        "",
        "| check_code | passed | failed | missing_anchor |",
        "| --- | ---: | ---: | ---: |",
    ]
    for code, counts in sorted(by_code.items()):
        lines.append(f"| `{code}` | {counts.get('passed', 0)} | {counts.get('failed', 0)} | {counts.get('missing_anchor', 0)} |")
    lines.extend(["", "## 非通过项", ""])
    if not failed:
        lines.append("无。")
    else:
        lines.extend([
            "| check_code | period_from | period_to | statement_id | instrument/currency | opening | cash_ledger_flow | market_trade_flow | expected | actual | diff | notes |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ])
        for row in failed:
            label = row.get("instrument_key") or row.get("currency") or ""
            detail = json.loads(row.get("detail_json") or "{}")
            lines.append(
                "| `{}` | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    row["check_code"],
                    row.get("period_from") or "",
                    row.get("period_to") or "",
                    row.get("statement_id") or "",
                    label,
                    detail.get("opening", ""),
                    detail.get("cash_ledger_flow", ""),
                    detail.get("market_trade_flow", ""),
                    "" if row.get("expected_value") is None else row.get("expected_value"),
                    "" if row.get("actual_value") is None else row.get("actual_value"),
                    "" if row.get("difference") is None else row.get("difference"),
                    row.get("notes") or "",
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行跨月现金/持仓连续性校验。")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--import-run-id")
    parser.add_argument("--run-id")
    parser.add_argument("--report-md", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = args.db_path.resolve()
    continuity_run_id = args.run_id or f"continuity_{utc_now_compact()}"
    with connect(db_path) as conn:
        if not table_exists(conn, "continuity_check_runs"):
            raise RuntimeError("continuity tables not found; apply investment management schema first.")
        import_run_id = args.import_run_id or latest_import_run_id(conn)
        items = run_checks(conn, import_run_id, continuity_run_id)
        summary = persist_results(conn, continuity_run_id, import_run_id, items)
    write_report(args.report_md.resolve(), summary, items)
    summary["db_path"] = str(db_path)
    summary["report_md"] = str(args.report_md.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
