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
    rounded = q2(value)
    if rounded == 0:
        return "0.00"
    return str(rounded)


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


def parse_close_event_id(value: Any) -> tuple[str | None, str]:
    raw = text(value)
    if ":" in raw:
        import_run_id, event_id = raw.split(":", 1)
        return import_run_id, event_id
    return None, raw


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


def load_canonical_name_map(conn: sqlite3.Connection) -> dict[str, str]:
    name_map: dict[str, str] = {}
    if table_exists(conn, "canonical_instruments"):
        for row in rows_as_dicts(
            conn,
            """
            SELECT canonical_instrument_id, canonical_symbol, canonical_name
            FROM canonical_instruments
            WHERE canonical_name IS NOT NULL
            """,
        ):
            canonical_name = text(row.get("canonical_name"))
            if canonical_name:
                name_map[text(row.get("canonical_instrument_id"))] = canonical_name
                name_map[text(row.get("canonical_symbol"))] = canonical_name

    if table_exists(conn, "platform_instrument_mappings") and table_exists(conn, "canonical_instruments"):
        for row in rows_as_dicts(
            conn,
            """
            SELECT m.platform_instrument_key, m.raw_symbol, m.raw_name,
                   c.canonical_symbol, c.canonical_name
            FROM platform_instrument_mappings m
            JOIN canonical_instruments c
              ON c.canonical_instrument_id = m.canonical_instrument_id
            WHERE m.mapping_status != 'ignored'
            """,
        ):
            canonical_name = text(row.get("canonical_name")) or text(row.get("raw_name"))
            if canonical_name:
                name_map[text(row.get("platform_instrument_key"))] = canonical_name
                name_map[text(row.get("raw_symbol"))] = canonical_name
                name_map[text(row.get("canonical_symbol"))] = canonical_name
    return {key: value for key, value in name_map.items() if key and value}


def should_replace_lot_name(raw_name: Any) -> bool:
    value = text(raw_name)
    upper = value.upper()
    return not value or upper.startswith("IPO ALLOTMENT") or upper in {"N/A", "UNKNOWN"}


def load_ipo_lots(conn: sqlite3.Connection, allocation_run_id: str | None) -> list[dict[str, Any]]:
    if not allocation_run_id or not table_exists(conn, "position_lots"):
        return []
    canonical_name_map = load_canonical_name_map(conn)
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
        canonical_name = (
            canonical_name_map.get(text(row.get("instrument_key")))
            or canonical_name_map.get(f"HK:{row['ipo_code']}")
            or canonical_name_map.get(text(row.get("instrument_code")))
        )
        if canonical_name and should_replace_lot_name(row.get("instrument_name")):
            row["instrument_name"] = canonical_name
        row["principal"] = dbnum(row_components.get("ipo_allotment_principal", Decimal("0")) + row_components.get("annual_ipo_net_cash_cost", Decimal("0")))
        row["application_handling_fee"] = dbnum(row_components.get("application_handling_fee", Decimal("0")))
        row["allotment_fee_or_levy"] = dbnum(row_components.get("ipo_allotment_fee_or_levy", Decimal("0")))
        allocation = allocations.get(lot_id, {})
        row["sold_quantity"] = dbnum(dec(allocation.get("sold_quantity")))
        row["net_sale_proceeds"] = dbnum(dec(allocation.get("proceeds")))
        row["sale_proceeds"] = row["net_sale_proceeds"]
        row["sold_cost"] = dbnum(dec(allocation.get("sold_cost")))
        row["open_cost"] = dbnum(dec(row.get("cost_basis_total")) - dec(allocation.get("sold_cost")))
        row["realized_pnl"] = dbnum(dec(allocation.get("realized_pnl")))
        row["latest_close_date"] = text(allocation.get("latest_close_date"))
        row["close_event_ids"] = text(allocation.get("close_event_ids"))
    return rows


def load_ipo_sale_allocations(
    conn: sqlite3.Connection,
    allocation_run_id: str | None,
    lot_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not allocation_run_id or not lot_rows or not table_exists(conn, "lot_allocations"):
        return []

    lot_by_id = {text(row["lot_id"]): row for row in lot_rows}
    placeholders = ", ".join("?" for _ in lot_by_id)
    rows = rows_as_dicts(
        conn,
        f"""
        SELECT *
        FROM lot_allocations
        WHERE allocation_run_id = ?
          AND lot_id IN ({placeholders})
        ORDER BY close_event_date, close_event_id, lot_id
        """,
        (allocation_run_id, *lot_by_id.keys()),
    )

    trade_rows = rows_as_dicts(
        conn,
        """
        SELECT import_run_id, trade_id, trade_date, settlement_date, currency,
               instrument_code_raw, instrument_symbol, instrument_name_raw,
               quantity, gross_amount, fee_total, net_cash_amount, source_refs
        FROM market_trades
        """,
    ) if table_exists(conn, "market_trades") else []
    trades_by_full_id = {f"{row['import_run_id']}:{row['trade_id']}": row for row in trade_rows}
    trades_by_short_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trade_rows:
        trades_by_short_id[text(row["trade_id"])].append(row)

    sale_rows: list[dict[str, Any]] = []
    for row in rows:
        lot = lot_by_id[text(row["lot_id"])]
        import_run_id, event_id = parse_close_event_id(row.get("close_event_id"))
        full_id = f"{import_run_id}:{event_id}" if import_run_id else event_id
        trade = trades_by_full_id.get(full_id)
        if trade is None:
            candidates = trades_by_short_id.get(event_id, [])
            trade = candidates[0] if len(candidates) == 1 else None

        quantity_allocated = dec(row.get("quantity_allocated"))
        trade_quantity = dec(trade.get("quantity")) if trade else Decimal("0")
        ratio = quantity_allocated / trade_quantity if trade_quantity else Decimal("0")
        sell_fee_allocated = q2(abs(dec(trade.get("fee_total"))) * ratio) if trade and trade.get("fee_total") not in (None, "") else Decimal("0")
        gross_sale_allocated = q2(dec(row.get("proceeds_allocated")) + sell_fee_allocated)

        sale_rows.append(
            {
                "lot_id": row["lot_id"],
                "ipo_code": lot.get("ipo_code", ""),
                "instrument_name": lot.get("instrument_name", ""),
                "close_event_id": row.get("close_event_id", ""),
                "close_event_date": row.get("close_event_date", ""),
                "quantity_allocated": dbnum(quantity_allocated),
                "gross_sale_amount_estimate": dbnum(gross_sale_allocated),
                "sell_fee_allocated_estimate": dbnum(sell_fee_allocated),
                "net_sale_proceeds": dbnum(dec(row.get("proceeds_allocated"))),
                "cost_allocated": dbnum(dec(row.get("cost_allocated"))),
                "realized_pnl": dbnum(dec(row.get("realized_pnl"))),
                "fee_source": "market_trades.fee_total allocated by quantity" if trade else "not_available",
                "trade_source_refs": text(trade.get("source_refs")) if trade else "",
            }
        )
    return sale_rows


def build_strategy_summary(
    *,
    cash_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    sale_rows: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lot_codes = {text(row.get("ipo_code")) for row in lot_rows if text(row.get("ipo_code"))}
    total_lot_cost = sum((dec(row.get("cost_basis_total")) for row in lot_rows), Decimal("0"))
    sold_lot_cost = sum((dec(row.get("sold_cost")) for row in lot_rows), Decimal("0"))
    open_lot_cost = q2(total_lot_cost - sold_lot_cost)
    net_sale_proceeds = sum((dec(row.get("net_sale_proceeds")) for row in lot_rows), Decimal("0"))
    realized_pnl = sum((dec(row.get("realized_pnl")) for row in lot_rows), Decimal("0"))
    sell_fee_estimate = sum((dec(row.get("sell_fee_allocated_estimate")) for row in sale_rows), Decimal("0"))
    gross_sale_estimate = sum((dec(row.get("gross_sale_amount_estimate")) for row in sale_rows), Decimal("0"))
    cash_only_rows = [row for row in cash_rows if text(row.get("ipo_code")) and text(row.get("ipo_code")) not in lot_codes]
    cash_only_net_amount = sum((dec(row.get("amount")) for row in cash_only_rows), Decimal("0"))
    failed_application_fee_expense = abs(sum(
        (dec(row.get("amount")) for row in cash_only_rows if row.get("cash_leg_type") == "application_handling_fee" and dec(row.get("amount")) < 0),
        Decimal("0"),
    ))
    strategy_realized_pnl = q2(realized_pnl + cash_only_net_amount)
    open_lot_count = sum(1 for row in lot_rows if dec(row.get("remaining_quantity")) > 0)
    sold_lot_count = sum(1 for row in lot_rows if dec(row.get("sold_quantity")) > 0)
    return [
        {"metric": "matched_ipo_allotment_lots", "value": str(len(lot_rows)), "notes": "已形成 IPO allotment lot 的中签记录。"},
        {"metric": "sold_ipo_lots", "value": str(sold_lot_count), "notes": "已产生卖出 allocation 的 IPO lot 数。"},
        {"metric": "open_ipo_lots", "value": str(open_lot_count), "notes": "仍未卖出的 IPO lot 数；其成本不进入 realized PnL。"},
        {"metric": "cash_only_ipo_codes", "value": str(len({text(row.get('ipo_code')) for row in cash_only_rows})), "notes": "有 IPO 现金腿但没有 allotment lot 的代码，通常是未中签。"},
        {"metric": "sold_lot_cost", "value": dbnum(sold_lot_cost), "notes": "已卖出 IPO lot 对应成本。"},
        {"metric": "open_lot_cost", "value": dbnum(open_lot_cost), "notes": "未卖出 IPO lot 剩余成本，不进入已实现收益。"},
        {"metric": "net_sale_proceeds", "value": dbnum(net_sale_proceeds), "notes": "卖出净现金流入；通常已扣卖出交易费用。"},
        {"metric": "gross_sale_amount_estimate", "value": dbnum(gross_sale_estimate), "notes": "按 market_trades.fee_total 估算还原的卖出成交额。"},
        {"metric": "sell_fee_allocated_estimate", "value": dbnum(sell_fee_estimate), "notes": "按成交数量分摊的卖出交易费用估算。"},
        {"metric": "realized_pnl_from_sold_lots", "value": dbnum(realized_pnl), "notes": "仅包含已卖出 IPO lot，不含未中签申购费。"},
        {"metric": "cash_only_ipo_net_amount", "value": dbnum(cash_only_net_amount), "notes": "没有中签 lot 的 IPO 现金净额；通常主要是未中签申购费。"},
        {"metric": "failed_application_fee_expense", "value": dbnum(failed_application_fee_expense), "notes": "未中签/无 lot IPO 的显式申购手续费支出。"},
        {"metric": "ipo_strategy_realized_pnl_after_cash_only", "value": dbnum(strategy_realized_pnl), "notes": "已卖出中签收益 + 无 lot IPO 现金净额；更接近打新策略已实现收益。"},
        {"metric": "review_items", "value": str(len(review_items)), "notes": "需要人工理解或确认的报告项。"},
    ]


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
    sale_rows: list[dict[str, Any]],
    strategy_summary: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
) -> str:
    total_cost = sum((dec(row.get("cost_basis_total")) for row in lot_rows), Decimal("0"))
    total_proceeds = sum((dec(row.get("net_sale_proceeds")) for row in lot_rows), Decimal("0"))
    total_pnl = sum((dec(row.get("realized_pnl")) for row in lot_rows), Decimal("0"))
    summary = [
        {"metric": "allocation_run_id", "value": allocation_run_id or ""},
        {"metric": "ipo_cash_legs", "value": str(len(cash_rows))},
        {"metric": "ipo_asset_events", "value": str(len(asset_rows))},
        {"metric": "ipo_allotment_lots", "value": str(len(lot_rows))},
        {"metric": "ipo_lot_total_cost", "value": dbnum(total_cost)},
        {"metric": "ipo_net_sale_proceeds", "value": dbnum(total_proceeds)},
        {"metric": "ipo_realized_pnl_from_sold_lots", "value": dbnum(total_pnl)},
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
        "sold_cost",
        "open_cost",
        "remaining_quantity",
        "net_sale_proceeds",
        "realized_pnl",
        "cost_basis_status",
    ]
    sale_columns = [
        "ipo_code",
        "close_event_date",
        "quantity_allocated",
        "gross_sale_amount_estimate",
        "sell_fee_allocated_estimate",
        "net_sale_proceeds",
        "cost_allocated",
        "realized_pnl",
        "fee_source",
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
            "## 策略口径摘要",
            "",
            md_table(strategy_summary, ["metric", "value", "notes"]),
            "",
            "说明：`ipo_realized_pnl_from_sold_lots` 只统计已经卖出的中签 lot；`ipo_strategy_realized_pnl_after_cash_only` 会进一步扣除没有中签 lot 的 IPO 现金净额，通常更接近“打新策略已实现收益”。未卖出的 IPO lot 只列在 `open_lot_cost`，不进入已实现收益。",
            "",
            "## IPO Allotment Lots",
            "",
            md_table(lot_rows, lot_columns),
            "",
            "## IPO 卖出费用审计",
            "",
            md_table(sale_rows, sale_columns),
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
        sale_rows = load_ipo_sale_allocations(conn, allocation_run_id, lot_rows)
        review_items = build_review_items(cash_rows=cash_rows, asset_rows=asset_rows, lot_rows=lot_rows)
        strategy_summary = build_strategy_summary(
            cash_rows=cash_rows,
            lot_rows=lot_rows,
            sale_rows=sale_rows,
            review_items=review_items,
        )
        report = build_report(
            allocation_run_id=allocation_run_id,
            cash_rows=cash_rows,
            asset_rows=asset_rows,
            lot_rows=lot_rows,
            sale_rows=sale_rows,
            strategy_summary=strategy_summary,
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
            "sold_cost",
            "open_cost",
            "net_sale_proceeds",
            "sale_proceeds",
            "realized_pnl",
            "cost_basis_status",
            "source_pk",
            "source_ref",
        ],
    )
    write_csv(
        output_dir / "ipo_sale_allocations.csv",
        sale_rows,
        [
            "lot_id",
            "ipo_code",
            "instrument_name",
            "close_event_id",
            "close_event_date",
            "quantity_allocated",
            "gross_sale_amount_estimate",
            "sell_fee_allocated_estimate",
            "net_sale_proceeds",
            "cost_allocated",
            "realized_pnl",
            "fee_source",
            "trade_source_refs",
        ],
    )
    write_csv(output_dir / "ipo_strategy_summary.csv", strategy_summary, ["metric", "value", "notes"])
    write_csv(output_dir / "ipo_review_items.csv", review_items, ["severity", "check_code", "ipo_code", "message"])
    report_path = output_dir / "ipo-report.md"
    report_path.write_text(report, encoding="utf-8")
    summary_by_metric = {row["metric"]: row["value"] for row in strategy_summary}
    return {
        "status": "ok",
        "db_path": str(db_path),
        "allocation_run_id": allocation_run_id,
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "cash_legs": len(cash_rows),
        "asset_events": len(asset_rows),
        "ipo_lots": len(lot_rows),
        "sale_allocations": len(sale_rows),
        "strategy_summary": summary_by_metric,
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
