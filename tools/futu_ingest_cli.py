#!/usr/bin/env python3
"""Futu PDF statement ingestion CLI.

Pipeline:
1. Extract text/tables from PDF statements into a raw cache.
2. Run futu_statement_parser_v1 against that cache.
3. Load parser outputs and validation anchors into SQLite.
4. Write a small ingest report and, optionally, a review workbook.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "futu_raw_fact_schema_v1.sql"
DEFAULT_MANAGEMENT_SCHEMA = WORKSPACE_ROOT / "schema" / "investment_management_schema_v1.sql"
DEFAULT_NODE_BIN = (
    Path(os.environ["NODE_BIN"])
    if os.environ.get("NODE_BIN")
    else (Path(shutil.which("node")) if shutil.which("node") else None)
)
EXTRACTOR_VERSION = "futu_pdf_cache_extractor_v1"
PARSER_MODULE_PATH = SCRIPT_DIR / "futu_statement_parser_v1.py"
WORKBOOK_SCRIPT = SCRIPT_DIR / "build_futu_review_workbook.mjs"

STATEMENT_RE = re.compile(r"20\d{4}")
STATEMENT_DATE_RE = re.compile(r"20\d{6}")
DATE_RE = re.compile(r"^20\d{2}/\d{2}/\d{2}$")
MONEY_RE = re.compile(r"^[+-]?[0-9,]+(?:\.\d+)?$")
STOCK_POSITION_RE = re.compile(
    r"^(.+?)\s+(FUTU OTC|SEHK|US|HKEX|XNDQ|BATO|NYSE|NASDAQ|AMEX|ARCA|EDGO)\s+([A-Z]{3})\s+"
    r"([+-]?[0-9,]+(?:\.\d+)?)\s+"
    r"([0-9,]+(?:\.\d+)?)\s+"
    r"(-|[0-9,]+(?:\.\d+)?)\s+"
    r"([+-]?[0-9,]+(?:\.\d+)?)"
    r"(?:\s+([+-]?[0-9,]+(?:\.\d+)?)\s+([+-]?[0-9,]+(?:\.\d+)?)\s+([0-9.]+))?$"
)
FUND_POSITION_RE = re.compile(
    r"^(HK\d{10}\s*\([^)]+\))\s+([A-Z]{3})\s+"
    r"([+-]?[0-9,]+(?:\.\d+)?)\s+"
    r"([0-9,]+(?:\.\d+)?)\s+"
    r"(20\d{2}/\d{2}/\d{2}|-)\s+"
    r"([+-]?[0-9,]+(?:\.\d+)?)\s+"
    r"([+-]?[0-9,]+(?:\.\d+)?)$"
)

PARSER_TABLES = [
    "cash_ledger_entries",
    "market_trades",
    "market_trade_fee_items",
    "fund_orders",
    "fund_order_cash_legs",
    "fund_transactions",
    "fund_transaction_fee_lines",
    "corporate_action_cash_legs",
    "asset_movement_events",
    "derivative_exercise_events",
    "financing_interest_events",
    "financing_interest_evidence_items",
    "stock_yield_daily_events",
    "stock_yield_cash_entries",
    "parser_issues",
    "unclassified_governance",
]

CACHE_TABLES = ["raw_statements", "position_snapshots", "statement_balance_snapshots"]

BALANCE_CURRENCIES = ["TOTAL_HKD", "HKD", "USD", "CNY", "JPY", "SGD"]
BALANCE_NUMBER_RE = re.compile(r"[+-]?[0-9,]+(?:\.\d+)?")
LEGACY_BALANCE_LABEL_MAP = {
    "證券市值": "security_market_value",
    "現金結餘": "cash_balance",
    "現金欠款": "cash_balance",
    "資產淨值": "net_asset_value",
    "初始保證金要求": "initial_margin_requirement",
    "可再開倉資金": "available_opening_funds",
    "維持保證金要求": "maintenance_margin_requirement",
}
LEGACY_BALANCE_PAIR_RE = re.compile(
    r"(期初|期末)?"
    r"(證券市值|現金結餘|現金欠款|資產淨值|初始保證金要求|可再開倉資金|維持保證金要求)"
    r"\s*[:：]?\s*([+-]?[0-9,]+(?:\.\d+)?)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_run_id() -> str:
    return datetime.now().strftime("futu_ingest_%Y%m%d_%H%M%S")


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value).replace("\n", " ")).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def period_from_filename(filename: str) -> str:
    for candidate in STATEMENT_DATE_RE.findall(filename):
        month = int(candidate[4:6])
        day = int(candidate[6:8])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return candidate[:6]
    match = STATEMENT_RE.search(filename)
    if not match:
        raise ValueError(f"无法从文件名识别结单周期: {filename}")
    return match.group(0)


def discover_pdfs(pdf_dir: Path, pattern: str) -> list[Path]:
    pdfs = sorted(path for path in pdf_dir.glob(pattern) if path.is_file() and path.suffix.lower() == ".pdf")
    if not pdfs:
        raise FileNotFoundError(f"没有找到 PDF: dir={pdf_dir}, glob={pattern}")
    return pdfs


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def resolve_prior_fund_amount_only_links(db_path: Path, parser_out_dir: Path) -> int:
    """Resolve complete fund orders against amount-only evidence already in the target DB."""
    if not db_path.exists():
        return 0
    fund_orders_path = parser_out_dir / "fund_orders.csv"
    parser_issues_path = parser_out_dir / "parser_issues.csv"
    fund_orders = read_csv(fund_orders_path)
    if not fund_orders:
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        prior_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT import_run_id, fund_order_id, statement_id, fund_order_type,
                       instrument_code, currency, order_date, fund_amount_abs,
                       cash_match_status, cash_match_source_refs, source_refs
                FROM fund_orders
                WHERE evidence_status = 'amount_only'
                  AND cash_match_status = 'matched_cash_leg'
                """
            )
        ]
    finally:
        conn.close()

    resolved: dict[str, dict[str, str]] = {}
    for order in fund_orders:
        if order.get("evidence_status") != "complete_detail":
            continue
        if order.get("cash_match_status") not in {"", "unmatched"}:
            continue
        matches = []
        for prior in prior_rows:
            if prior["fund_order_type"] != order.get("fund_order_type"):
                continue
            if prior["instrument_code"] != order.get("instrument_code"):
                continue
            if prior["currency"] != order.get("currency"):
                continue
            if prior["order_date"] != order.get("order_date"):
                continue
            if abs(float(prior["fund_amount_abs"]) - float(order.get("fund_amount_abs") or "0")) > 0.01:
                continue
            matches.append(prior)
        if len(matches) != 1:
            continue
        prior = matches[0]
        order["cash_match_status"] = "matched_prior_amount_only"
        order["cash_match_source_refs"] = (
            f"{prior['import_run_id']}:{prior['fund_order_id']}:{prior['cash_match_source_refs']}"
        )
        resolved[order["fund_order_id"]] = prior

    if not resolved:
        return 0

    write_csv(fund_orders_path, fund_orders, list(fund_orders[0].keys()))

    issues = read_csv(parser_issues_path)
    if issues:
        for issue in issues:
            if issue.get("issue_type") != "fund_order_without_cash_leg":
                continue
            matched_order_id = next(
                (order_id for order_id in resolved if order_id in issue.get("message", "")),
                "",
            )
            if not matched_order_id:
                continue
            prior = resolved[matched_order_id]
            issue["severity"] = "info"
            issue["status"] = "resolved_prior_amount_only"
            issue["message"] = (
                f"Fund order {matched_order_id} matched prior amount-only order "
                f"{prior['import_run_id']}:{prior['fund_order_id']}; do not create a duplicate cash leg."
            )
        write_csv(parser_issues_path, issues, list(issues[0].keys()))

    return len(resolved)


def clean_row(row: list[Any] | None) -> list[str]:
    return [normalize_space(cell) for cell in (row or [])]


def classify_page_section(text: str) -> str:
    if "資資金金進進出出" in text:
        return "cash_activity"
    if "資資產產進進出出" in text or "資資產進出" in text:
        return "asset_movement"
    if "借借出出證證券券總總覽覽" in text:
        return "stock_yield"
    if "期期初初概概覽覽" in text:
        return "opening_snapshot"
    if "期期末末概概覽覽" in text:
        return "ending_snapshot"
    return "unclassified"


def is_cash_activity_row(row: list[str]) -> bool:
    return (
        len(row) >= 6
        and bool(DATE_RE.match(row[0]))
        and row[1] in {"增加", "減少"}
        and row[3] in {"HKD", "USD", "CNY"}
        and bool(MONEY_RE.match(row[4]))
    )


def asset_row_from_table(
    statement: dict[str, Any],
    page_number: int,
    table_index: int,
    row_index: int,
    row: list[str],
) -> dict[str, Any] | None:
    date_index = next((i for i, cell in enumerate(row) if DATE_RE.match(cell)), -1)
    if date_index < 0 or date_index + 7 >= len(row):
        return None
    cells = row[date_index:]
    if cells[1] not in {"增加", "減少"} or cells[4] not in {"HKD", "USD", "CNY"}:
        return None
    if not MONEY_RE.match(cells[5]) or not MONEY_RE.match(cells[6]):
        return None
    return {
        "statement_id": statement["statement_id"],
        "period": statement["period"],
        "filename": statement["filename"],
        "page": page_number,
        "table_index": table_index,
        "row_index": row_index,
        "section": "asset_movement",
        "event_date": cells[0],
        "direction": cells[1],
        "event_type_raw": cells[2],
        "code_name": cells[3],
        "currency": cells[4],
        "quantity": cells[5],
        "amount": cells[6],
        "description": cells[7],
    }


def stock_yield_row_from_table(
    statement: dict[str, Any],
    page_number: int,
    table_index: int,
    row_index: int,
    row: list[str],
) -> dict[str, Any] | None:
    if len(row) < 12 or not DATE_RE.match(row[0]):
        return None
    if row[2] not in {"US", "SEHK", "HKEX", "XNDQ", "BATO", "NYSE", "NASDAQ", "AMEX", "ARCA"}:
        return None
    if row[3] not in {"HKD", "USD", "CNY"}:
        return None
    return {
        "statement_id": statement["statement_id"],
        "period": statement["period"],
        "filename": statement["filename"],
        "page": page_number,
        "table_index": table_index,
        "row_index": row_index,
        "section": "stock_yield",
        "event_date": row[0],
        "code_name": row[1],
        "market": row[2],
        "currency": row[3],
        "interest_type": row[4],
        "quantity": row[5],
        "settlement_amount": row[6],
        "collateral_amount": row[7],
        "annual_rate": row[8],
        "interest": row[9],
        "cumulative_interest": row[10],
        "income_month": row[11],
    }


def current_snapshot_section(line: str, current: str | None) -> str | None:
    if "期期初初概概覽覽--股股票票和和股股票票期期權權" in line or "期初概覽-股票和股票期權" in line:
        return "opening_stock_positions"
    if "期期初初概概覽覽--基基金金" in line or "期初概覽-基金" in line:
        return "opening_fund_positions"
    if "期期末末概概覽覽--股股票票和和股股票票期期權權" in line or "期末概覽-股票和股票期權" in line:
        return "ending_stock_positions"
    if "期期末末概概覽覽--基基金金" in line or "期末概覽-基金" in line:
        return "ending_fund_positions"
    if line in {"交交易易", "交易"}:
        return None
    reset_markers = [
        "資資金金進進出出",
        "資資產產進進出出",
        "借借出出證證券券總總覽覽",
        "資資產產淨淨值值",
    ]
    if any(marker in line for marker in reset_markers):
        return None
    return current


def snapshot_type(section: str) -> str:
    return "opening" if section.startswith("opening") else "ending"


def position_rows_from_text(statement: dict[str, Any], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    section: str | None = None
    for page in pages:
        page_number = page["page_number"]
        for line_no, raw_line in enumerate(page.get("text", "").splitlines(), start=1):
            line = normalize_space(raw_line)
            section = current_snapshot_section(line, section)
            if not section:
                continue
            if "代代碼碼名名稱稱" in line:
                continue
            stock_match = STOCK_POSITION_RE.match(line)
            if stock_match and "stock" in section:
                (
                    code_name,
                    market,
                    currency,
                    quantity,
                    price,
                    multiplier,
                    market_value,
                    initial_margin_requirement,
                    maintenance_margin_requirement,
                    maintenance_margin_rate,
                ) = stock_match.groups()
                rows.append(
                    {
                        "statement_id": statement["statement_id"],
                        "period": statement["period"],
                        "filename": statement["filename"],
                        "page": page_number,
                        "table_index": 0,
                        "row_index": line_no,
                        "section": section,
                        "snapshot_type": snapshot_type(section),
                        "asset_category": "stock_or_option",
                        "code_name": code_name,
                        "market": market,
                        "currency": currency,
                        "quantity": quantity,
                        "price": price,
                        "multiplier": "" if multiplier == "-" else multiplier,
                        "market_value": market_value,
                        "price_date": "",
                        "pending_amount": "",
                        "initial_margin_requirement": initial_margin_requirement or "",
                        "maintenance_margin_requirement": maintenance_margin_requirement or "",
                        "maintenance_margin_rate": maintenance_margin_rate or "",
                    }
                )
                continue
            fund_match = FUND_POSITION_RE.match(line)
            if fund_match and "fund" in section:
                code_name, currency, quantity, price, price_date, pending_amount, market_value = fund_match.groups()
                rows.append(
                    {
                        "statement_id": statement["statement_id"],
                        "period": statement["period"],
                        "filename": statement["filename"],
                        "page": page_number,
                        "table_index": 0,
                        "row_index": line_no,
                        "section": section,
                        "snapshot_type": snapshot_type(section),
                        "asset_category": "fund",
                        "code_name": code_name,
                        "market": "",
                        "currency": currency,
                        "quantity": quantity,
                        "price": price,
                        "multiplier": "",
                        "market_value": market_value,
                        "price_date": "" if price_date == "-" else price_date,
                        "pending_amount": pending_amount,
                        "initial_margin_requirement": "",
                        "maintenance_margin_requirement": "",
                        "maintenance_margin_rate": "",
                    }
                )
    return rows


def balance_section_from_line(line: str, current: str | None) -> str | None:
    if all(marker in line for marker in ["期", "初", "資", "產", "淨", "值", "總", "覽"]):
        return "opening"
    if all(marker in line for marker in ["期", "末", "資", "產", "淨", "值", "總", "覽"]):
        return "ending"
    if "製備日期" in line or "制备日期" in line:
        return None
    return current


def is_cash_balance_line(line: str) -> bool:
    return all(marker in line for marker in ["現", "金", "結", "餘"])


def statement_balance_rows_from_text(statement: dict[str, Any], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    section: str | None = None
    for page in pages:
        page_number = page["page_number"]
        for line_no, raw_line in enumerate(page.get("text", "").splitlines(), start=1):
            line = normalize_space(raw_line)
            section = balance_section_from_line(line, section)
            if section not in {"opening", "ending"} or not is_cash_balance_line(line):
                continue
            values = BALANCE_NUMBER_RE.findall(line)
            if len(values) < len(BALANCE_CURRENCIES):
                continue
            for currency, reported_balance in zip(BALANCE_CURRENCIES, values[: len(BALANCE_CURRENCIES)]):
                snapshot_type = f"{section}_cash_balance"
                rows.append(
                    {
                        "balance_snapshot_id": f"{statement['statement_id']}_{snapshot_type}_{currency}",
                        "account_id": "futu_hk_main",
                        "statement_id": statement["statement_id"],
                        "period": statement["period"],
                        "snapshot_type": snapshot_type,
                        "currency": currency,
                        "reported_balance": reported_balance,
                        "source_ref": f"{statement['statement_id']}/p{page_number}/text_line:{line_no}",
                        "status": "active",
                        "notes": "parsed_from_statement_net_value_overview",
                    }
                )
    return rows


def legacy_position_snapshot_type(table: list[list[str]], page_text: str) -> str:
    table_text = normalize_space(" ".join(cell for row in table for cell in row))
    if "期初證券市值" in table_text:
        return "opening"
    if "期末證券市值" in table_text:
        return "ending"
    header = normalize_space(" ".join(table[0])) if table else ""
    if "昨收" in header:
        return "opening"
    if "收市" in header:
        return "ending"
    if "期初總覽" in page_text:
        return "opening"
    if "期末總覽" in page_text:
        return "ending"
    return ""


def is_legacy_position_table(table: list[list[str]], page_text: str) -> bool:
    if not table:
        return False
    first = table[0]
    first_text = normalize_space(" ".join(first))
    if len(first) >= 4 and first[0] == "股票" and "持有數量" in first_text and "市值" in first_text:
        return True
    if any("期初證券市值" in normalize_space(" ".join(row)) for row in table):
        return True
    if any("期末證券市值" in normalize_space(" ".join(row)) for row in table):
        return True
    return bool(table and is_legacy_position_row(first) and ("期初總覽" in page_text or "期末總覽" in page_text))


def is_legacy_position_row(row: list[str]) -> bool:
    if len(row) < 4:
        return False
    if not row[0] or row[0] == "股票" or "證券市值" in row[0]:
        return False
    return bool(MONEY_RE.match(row[1]) and MONEY_RE.match(row[2]) and MONEY_RE.match(row[3]))


def legacy_position_rows_from_tables(statement: dict[str, Any], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages:
        page_number = page["page_number"]
        page_text = normalize_space(page.get("text", ""))
        for table_index, table in enumerate(page.get("tables", []), start=1):
            if not is_legacy_position_table(table, page_text):
                continue
            snapshot = legacy_position_snapshot_type(table, page_text)
            if not snapshot:
                continue
            for row_index, row in enumerate(table, start=1):
                if not is_legacy_position_row(row):
                    continue
                rows.append(
                    {
                        "statement_id": statement["statement_id"],
                        "period": statement["period"],
                        "filename": statement["filename"],
                        "page": page_number,
                        "table_index": table_index,
                        "row_index": row_index,
                        "section": f"{snapshot}_stock_positions_legacy",
                        "snapshot_type": snapshot,
                        "asset_category": "stock_or_derivative",
                        "code_name": row[0],
                        "market": "SEHK",
                        "currency": "HKD",
                        "quantity": row[1],
                        "price": row[2],
                        "multiplier": "",
                        "market_value": row[3],
                        "price_date": "",
                        "pending_amount": "",
                        "initial_margin_requirement": row[4] if len(row) > 4 and MONEY_RE.match(row[4]) else "",
                        "maintenance_margin_requirement": row[5] if len(row) > 5 and MONEY_RE.match(row[5]) else "",
                        "maintenance_margin_rate": row[6] if len(row) > 6 else "",
                    }
                )
    return rows


def legacy_balance_rows_from_tables(statement: dict[str, Any], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def append_balance_rows_from_text(
        *,
        text: str,
        source_ref: str,
        default_scope: str,
        notes: str,
    ) -> None:
        current_scope = "ending" if "期末" in text else "opening" if "期初" in text else default_scope
        for prefix, raw_label, value in LEGACY_BALANCE_PAIR_RE.findall(text):
            if prefix:
                current_scope = "opening" if prefix == "期初" else "ending"
            if not current_scope:
                continue
            label = LEGACY_BALANCE_LABEL_MAP[raw_label]
            key = (current_scope, label)
            if key in seen:
                continue
            seen.add(key)
            snapshot_type = f"{current_scope}_{label}"
            rows.append(
                {
                    "balance_snapshot_id": f"{statement['statement_id']}_{snapshot_type}_HKD",
                    "account_id": "futu_hk_main",
                    "statement_id": statement["statement_id"],
                    "period": statement["period"],
                    "snapshot_type": snapshot_type,
                    "currency": "HKD",
                    "reported_balance": value,
                    "source_ref": source_ref,
                    "status": "active",
                    "notes": notes,
                }
            )

    for page in pages:
        page_number = page["page_number"]
        page_text = normalize_space(page.get("text", ""))
        if "港股保證金賬戶月結單" in page_text or "港股現金賬戶月結單" in page_text:
            append_balance_rows_from_text(
                text=page_text,
                source_ref=f"{statement['statement_id']}/p{page_number}/text:legacy_balance",
                default_scope="ending",
                notes="parsed_from_legacy_monthly_text_summary",
            )
        for table_index, table in enumerate(page.get("tables", []), start=1):
            for row_index, row in enumerate(table, start=1):
                text = normalize_space(" ".join(row))
                if "證券市值" not in text and "現金結餘" not in text and "資產淨值" not in text:
                    continue
                append_balance_rows_from_text(
                    text=text,
                    source_ref=f"{statement['statement_id']}/p{page_number}/table:{table_index}/row:{row_index}",
                    default_scope="",
                    notes="parsed_from_legacy_monthly_position_summary",
                )
    return rows


def is_legacy_asset_movement_row(row: list[str]) -> bool:
    if len(row) < 6:
        return False
    if row[0] not in {"存入股票", "提取股票"}:
        return False
    return bool(DATE_RE.match(row[1]) and MONEY_RE.match(row[3]))


def legacy_asset_movement_rows_from_tables(statement: dict[str, Any], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages:
        page_number = page["page_number"]
        for table_index, table in enumerate(page.get("tables", []), start=1):
            for row_index, row in enumerate(table, start=1):
                if not is_legacy_asset_movement_row(row):
                    continue
                rows.append(
                    {
                        "statement_id": statement["statement_id"],
                        "period": statement["period"],
                        "filename": statement["filename"],
                        "page": page_number,
                        "table_index": table_index,
                        "row_index": row_index,
                        "section": "legacy_asset_movement",
                        "event_date": row[1],
                        "direction": "增加" if row[3].startswith("+") or row[0] == "存入股票" else "減少",
                        "event_type_raw": row[0],
                        "code_name": row[2],
                        "currency": "HKD",
                        "quantity": row[3],
                        "amount": "0.00",
                        "description": row[5],
                    }
                )
    return rows


def extract_pdf_cache(pdf_dir: Path, pattern: str, cache_dir: Path) -> dict[str, Any]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境缺少 pdfplumber；请用 Codex bundled Python 运行本 CLI。") from exc

    discovered_pdfs = discover_pdfs(pdf_dir, pattern)
    seen_sha: dict[str, Path] = {}
    duplicate_files: list[dict[str, str]] = []
    pdfs: list[tuple[Path, str]] = []
    for pdf_path in discovered_pdfs:
        pdf_sha = sha256_file(pdf_path)
        if pdf_sha in seen_sha:
            duplicate_files.append(
                {
                    "filename": pdf_path.name,
                    "source_file": str(pdf_path),
                    "duplicate_of": seen_sha[pdf_sha].name,
                    "sha256": pdf_sha,
                }
            )
            continue
        seen_sha[pdf_sha] = pdf_path
        pdfs.append((pdf_path, pdf_sha))
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_statements: list[dict[str, Any]] = []
    position_snapshots: list[dict[str, Any]] = []
    statement_balance_snapshots: list[dict[str, Any]] = []
    asset_movements: list[dict[str, Any]] = []
    stock_yield_events: list[dict[str, Any]] = []
    table_inventory: list[dict[str, Any]] = []
    raw_table_rows: list[dict[str, Any]] = []
    unclassified_tables: list[dict[str, Any]] = []

    period_counts: dict[str, int] = {}
    for pdf_path, _pdf_sha in pdfs:
        period = period_from_filename(pdf_path.name)
        period_counts[period] = period_counts.get(period, 0) + 1

    used_periods: dict[str, int] = {}
    for pdf_path, pdf_sha in pdfs:
        period = period_from_filename(pdf_path.name)
        used_periods[period] = used_periods.get(period, 0) + 1
        statement_id = period if period_counts[period] == 1 else f"{period}_{used_periods[period]:02d}"
        statement = {
            "statement_id": statement_id,
            "period": period,
            "filename": pdf_path.name,
            "sha256": pdf_sha,
            "source_file": str(pdf_path),
        }
        pages: list[dict[str, Any]] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            statement["pages"] = len(pdf.pages)
            for page_index, page in enumerate(pdf.pages, start=1):
                text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                tables = page.extract_tables() or []
                cleaned_tables = [[clean_row(row) for row in table] for table in tables]
                pages.append(
                    {
                        "page_number": page_index,
                        "width": page.width,
                        "height": page.height,
                        "text": text,
                        "text_len": len(text),
                        "tables": cleaned_tables,
                        "table_count": len(cleaned_tables),
                        "table_shapes": [
                            {
                                "rows": len(table),
                                "cols": max((len(row) for row in table), default=0),
                            }
                            for table in cleaned_tables
                        ],
                    }
                )
                page_section = classify_page_section(text)
                for table_index, table in enumerate(cleaned_tables, start=1):
                    table_inventory.append(
                        {
                            "statement_id": statement_id,
                            "period": period,
                            "filename": pdf_path.name,
                            "page": page_index,
                            "table_index": table_index,
                            "section": page_section,
                            "rows": len(table),
                            "cols": max((len(row) for row in table), default=0),
                        }
                    )
                    for row_index, row in enumerate(table, start=1):
                        raw_table_rows.append(
                            {
                                "statement_id": statement_id,
                                "period": period,
                                "filename": pdf_path.name,
                                "page": page_index,
                                "table_index": table_index,
                                "row_index": row_index,
                                "section": page_section,
                                "cols": len(row),
                                "raw_row": repr(row),
                            }
                        )
                        asset = asset_row_from_table(statement, page_index, table_index, row_index, row)
                        stock_yield = stock_yield_row_from_table(statement, page_index, table_index, row_index, row)
                        if asset:
                            asset_movements.append(asset)
                            continue
                        if stock_yield:
                            stock_yield_events.append(stock_yield)
                            continue
                        if is_cash_activity_row(row):
                            continue
                        if any(row) and page_section in {"cash_activity", "asset_movement", "stock_yield"}:
                            unclassified_tables.append(
                                {
                                    "statement_id": statement_id,
                                    "period": period,
                                    "filename": pdf_path.name,
                                    "page": page_index,
                                    "table_index": table_index,
                                    "row_index": row_index,
                                    "section": page_section,
                                    "cols": len(row),
                                    "raw_row": repr(row),
                                }
                            )
        raw_payload = {
            "source_file": str(pdf_path),
            "filename": pdf_path.name,
            "sha256": statement["sha256"],
            "bytes": pdf_path.stat().st_size,
            "pypdf_pages": statement["pages"],
            "metadata": {},
            "pages": pages,
        }
        (cache_dir / pdf_path.name.replace(".pdf", ".raw.json")).write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raw_statements.append(statement)
        position_snapshots.extend(position_rows_from_text(statement, pages))
        position_snapshots.extend(legacy_position_rows_from_tables(statement, pages))
        statement_balance_snapshots.extend(statement_balance_rows_from_text(statement, pages))
        statement_balance_snapshots.extend(legacy_balance_rows_from_tables(statement, pages))
        asset_movements.extend(legacy_asset_movement_rows_from_tables(statement, pages))

    write_csv(
        cache_dir / "raw_statements.csv",
        raw_statements,
        ["statement_id", "period", "filename", "sha256", "pages", "source_file"],
    )
    write_csv(
        cache_dir / "position_snapshots.csv",
        position_snapshots,
        [
            "statement_id",
            "period",
            "filename",
            "page",
            "table_index",
            "row_index",
            "section",
            "snapshot_type",
            "asset_category",
            "code_name",
            "market",
            "currency",
            "quantity",
            "price",
            "multiplier",
            "market_value",
            "price_date",
            "pending_amount",
            "initial_margin_requirement",
            "maintenance_margin_requirement",
            "maintenance_margin_rate",
        ],
    )
    write_csv(
        cache_dir / "statement_balance_snapshots.csv",
        statement_balance_snapshots,
        [
            "balance_snapshot_id",
            "account_id",
            "statement_id",
            "period",
            "snapshot_type",
            "currency",
            "reported_balance",
            "source_ref",
            "status",
            "notes",
        ],
    )
    write_csv(
        cache_dir / "asset_movements.csv",
        asset_movements,
        [
            "statement_id",
            "period",
            "filename",
            "page",
            "table_index",
            "row_index",
            "section",
            "event_date",
            "direction",
            "event_type_raw",
            "code_name",
            "currency",
            "quantity",
            "amount",
            "description",
        ],
    )
    write_csv(
        cache_dir / "stock_yield_events.csv",
        stock_yield_events,
        [
            "statement_id",
            "period",
            "filename",
            "page",
            "table_index",
            "row_index",
            "section",
            "event_date",
            "code_name",
            "market",
            "currency",
            "interest_type",
            "quantity",
            "settlement_amount",
            "collateral_amount",
            "annual_rate",
            "interest",
            "cumulative_interest",
            "income_month",
        ],
    )
    write_csv(
        cache_dir / "table_inventory.csv",
        table_inventory,
        ["statement_id", "period", "filename", "page", "table_index", "section", "rows", "cols"],
    )
    write_csv(
        cache_dir / "raw_table_rows.csv",
        raw_table_rows,
        ["statement_id", "period", "filename", "page", "table_index", "row_index", "section", "cols", "raw_row"],
    )
    write_csv(
        cache_dir / "unclassified_tables.csv",
        unclassified_tables,
        ["statement_id", "period", "filename", "page", "table_index", "row_index", "section", "cols", "raw_row"],
    )
    manifest = {
        "extractor_version": EXTRACTOR_VERSION,
        "pdf_dir": str(pdf_dir),
        "pdf_glob": pattern,
        "statement_count": len(raw_statements),
        "files": [row["filename"] for row in raw_statements],
        "duplicate_count": len(duplicate_files),
        "duplicates_skipped": duplicate_files,
        "counts": {
            "position_snapshots": len(position_snapshots),
            "statement_balance_snapshots": len(statement_balance_snapshots),
            "asset_movements": len(asset_movements),
            "stock_yield_events": len(stock_yield_events),
            "unclassified_tables": len(unclassified_tables),
        },
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_parser_module() -> Any:
    spec = importlib.util.spec_from_file_location("futu_statement_parser_v1", PARSER_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 parser: {PARSER_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_parser(cache_dir: Path, parser_out_dir: Path) -> dict[str, Any]:
    parser_out_dir.mkdir(parents=True, exist_ok=True)
    module = load_parser_module()
    return module.run(cache_dir, parser_out_dir)


def sqlite_columns(conn: sqlite3.Connection, table_name: str) -> dict[str, str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1]: row[2].upper() for row in rows}


def sqlite_object_exists(conn: sqlite3.Connection, object_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (object_name,),
    ).fetchone()
    return row is not None


def apply_management_schema(conn: sqlite3.Connection, management_schema_path: Path | None) -> None:
    if management_schema_path is None:
        return
    conn.executescript(management_schema_path.read_text(encoding="utf-8"))


def bootstrap_statement_accounts(conn: sqlite3.Connection, import_run_id: str) -> int:
    if not sqlite_object_exists(conn, "statement_accounts"):
        return 0
    before = conn.execute(
        "SELECT COUNT(*) FROM statement_accounts WHERE import_run_id = ?",
        (import_run_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT OR IGNORE INTO statement_accounts (
          import_run_id, statement_id, account_id, link_source, confidence, notes
        )
        SELECT
          import_run_id,
          statement_id,
          'futu_hk_main',
          'default_futu_hk_main',
          'inferred',
          '由富途结单导入批次默认挂接。'
        FROM raw_statements
        WHERE import_run_id = ?
        """,
        (import_run_id,),
    )
    after = conn.execute(
        "SELECT COUNT(*) FROM statement_accounts WHERE import_run_id = ?",
        (import_run_id,),
    ).fetchone()[0]
    return int(after - before)


def coerce_sql_value(value: str, column_type: str) -> Any:
    if value == "":
        return None
    if "INT" in column_type:
        try:
            return int(float(value.replace(",", "").replace("+", "")))
        except ValueError:
            return value
    if any(kind in column_type for kind in ["NUMERIC", "REAL", "DOUBLE", "FLOAT", "DECIMAL"]):
        try:
            return float(value.replace(",", "").replace("+", ""))
        except ValueError:
            return value
    return value


def insert_csv_table(conn: sqlite3.Connection, table_name: str, path: Path, import_run_id: str) -> int:
    rows = read_csv(path)
    if not rows:
        return 0
    table_columns = sqlite_columns(conn, table_name)
    insert_columns = ["import_run_id"] + [
        col for col in rows[0].keys() if col in table_columns and col != "import_run_id"
    ]
    placeholders = ", ".join("?" for _ in insert_columns)
    sql = f"INSERT INTO {table_name} ({', '.join(insert_columns)}) VALUES ({placeholders})"
    values = []
    for row in rows:
        values.append(
            [
                import_run_id if col == "import_run_id" else coerce_sql_value(row.get(col, ""), table_columns[col])
                for col in insert_columns
            ]
        )
    conn.executemany(sql, values)
    return len(rows)


def load_acceptance_tests(conn: sqlite3.Connection, import_run_id: str, acceptance_report: dict[str, Any]) -> int:
    rows = []
    for test in acceptance_report.get("tests", []):
        rows.append(
            (
                import_run_id,
                test.get("code", ""),
                json.dumps(test.get("actual", ""), ensure_ascii=False),
                json.dumps(test.get("expected", ""), ensure_ascii=False),
                1 if test.get("passed") else 0,
            )
        )
    conn.executemany(
        """
        INSERT INTO acceptance_tests (import_run_id, code, actual, expected, passed)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def load_sqlite(
    db_path: Path,
    schema_path: Path,
    import_run_id: str,
    pdf_dir: Path,
    pdf_glob: str,
    cache_dir: Path,
    parser_out_dir: Path,
    review_xlsx: Path | None,
    acceptance_report: dict[str, Any],
    replace_db: bool,
    append_db: bool,
    management_schema_path: Path | None,
) -> dict[str, int]:
    if db_path.exists():
        if not replace_db and not append_db:
            raise FileExistsError(f"SQLite 已存在，请换路径，或使用 --replace-db / --append-db: {db_path}")
        if replace_db and append_db:
            raise ValueError("--replace-db 与 --append-db 不能同时使用")
        if replace_db:
            db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_sql)
        apply_management_schema(conn, management_schema_path)
        raw_statements = read_csv(cache_dir / "raw_statements.csv")
        conn.execute(
            """
            INSERT INTO import_runs (
              import_run_id, created_at, parser_version, extractor_version, status,
              pdf_dir, pdf_glob, cache_dir, parser_out_dir, db_path, review_xlsx_path,
              statement_count, acceptance_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_run_id,
                utc_now(),
                acceptance_report.get("parser_version", "futu_statement_parser_v1"),
                EXTRACTOR_VERSION,
                "loaded",
                str(pdf_dir),
                pdf_glob,
                str(cache_dir),
                str(parser_out_dir),
                str(db_path),
                "" if review_xlsx is None else str(review_xlsx),
                len(raw_statements),
                acceptance_report.get("status", "unknown"),
                "raw fact layer only; no tax/lot/treatment calculations",
            ),
        )
        counts: dict[str, int] = {}
        for table_name in CACHE_TABLES:
            counts[table_name] = insert_csv_table(conn, table_name, cache_dir / f"{table_name}.csv", import_run_id)
        for table_name in PARSER_TABLES:
            counts[table_name] = insert_csv_table(conn, table_name, parser_out_dir / f"{table_name}.csv", import_run_id)
        statement_account_links = bootstrap_statement_accounts(conn, import_run_id)
        if statement_account_links:
            counts["statement_accounts"] = statement_account_links
        counts["acceptance_tests"] = load_acceptance_tests(conn, import_run_id, acceptance_report)
        conn.executemany(
            """
            INSERT INTO ingest_table_counts (import_run_id, table_name, row_count)
            VALUES (?, ?, ?)
            """,
            [(import_run_id, table_name, row_count) for table_name, row_count in sorted(counts.items())],
        )
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_review_workbook(
    parser_out_dir: Path,
    output_dir: Path,
    review_xlsx: Path,
    node_bin: Path | None,
    title: str,
    scope: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if node_bin is None:
        resolved = shutil.which("node")
        if resolved is None:
            raise RuntimeError("未找到 node；请传入 --node-bin 或使用 Codex bundled Node。")
        node_bin = Path(resolved)
    cmd = [
        str(node_bin),
        str(WORKBOOK_SCRIPT),
        "--source-dir",
        str(parser_out_dir),
        "--output-dir",
        str(output_dir),
        "--output-xlsx",
        str(review_xlsx),
        "--title",
        title,
        "--scope",
        scope,
    ]
    subprocess.run(cmd, cwd=str(SCRIPT_DIR), check=True)


def summarize_pending_issues(parser_out_dir: Path) -> dict[str, Any]:
    rows = read_csv(parser_out_dir / "parser_issues.csv")
    pending = [
        row
        for row in rows
        if row.get("severity") == "needs_review" or row.get("status") in {"open", "ambiguous", "unmatched"}
    ]
    by_type: dict[str, int] = {}
    for row in pending:
        by_type[row.get("issue_type", "")] = by_type.get(row.get("issue_type", ""), 0) + 1
    return {"pending_count": len(pending), "pending_by_type": by_type}


def write_ingest_reports(
    report_dir: Path,
    import_run_id: str,
    cache_manifest: dict[str, Any],
    acceptance_report: dict[str, Any],
    db_counts: dict[str, int],
    db_path: Path,
    review_xlsx: Path | None,
    parser_out_dir: Path,
) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    pending = summarize_pending_issues(parser_out_dir)
    report = {
        "import_run_id": import_run_id,
        "created_at": utc_now(),
        "status": "passed" if acceptance_report.get("status") == "passed" and pending["pending_count"] == 0 else "needs_review",
        "cache_manifest": cache_manifest,
        "acceptance_status": acceptance_report.get("status"),
        "parser_counts": acceptance_report.get("counts", {}),
        "db_counts": db_counts,
        "pending_issues": pending,
        "db_path": str(db_path),
        "review_xlsx": "" if review_xlsx is None else str(review_xlsx),
    }
    (report_dir / "ingest_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# 富途结单导入报告：{import_run_id}",
        "",
        f"- 导入状态：`{report['status']}`",
        f"- Parser 验收状态：`{report['acceptance_status']}`",
        f"- 结单数量：`{cache_manifest.get('statement_count', 0)}`",
        f"- SQLite：`{db_path}`",
        f"- 审阅工作簿：`{report['review_xlsx'] or '未生成'}`",
        f"- 待复核问题数：`{pending['pending_count']}`",
        "",
        "## 表行数",
        "",
        "| 表 | 行数 |",
        "| --- | ---: |",
    ]
    for table_name, row_count in sorted(db_counts.items()):
        lines.append(f"| `{table_name}` | {row_count} |")
    if pending["pending_by_type"]:
        lines.extend(["", "## 待复核类型", "", "| 类型 | 数量 |", "| --- | ---: |"])
        for issue_type, count in sorted(pending["pending_by_type"].items()):
            lines.append(f"| `{issue_type}` | {count} |")
    (report_dir / "ingest_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入富途 PDF 结单到原始事实 SQLite。")
    parser.add_argument("--pdf-dir", required=True, type=Path, help="PDF 结单所在目录。")
    parser.add_argument("--glob", default="*.pdf", help="PDF 文件 glob，默认 *.pdf。")
    parser.add_argument("--work-dir", type=Path, help="本次运行目录；默认写入 workspace cache/futu-ingest-runs/<run_id>。")
    parser.add_argument("--run-id", default=local_run_id(), help="导入运行 ID。")
    parser.add_argument("--db-path", type=Path, help="SQLite 输出路径；默认 work-dir/futu_raw_fact.sqlite。")
    parser.add_argument("--schema-path", default=DEFAULT_SCHEMA, type=Path, help="SQLite schema SQL 文件。")
    parser.add_argument(
        "--management-schema-path",
        default=DEFAULT_MANAGEMENT_SCHEMA,
        type=Path,
        help="投资数据库管理层 schema SQL 文件；默认自动应用。",
    )
    parser.add_argument("--skip-management-schema", action="store_true", help="只写入 raw fact schema，不应用管理层。")
    parser.add_argument("--review-xlsx", type=Path, help="审阅 Excel 输出路径；默认 work-dir/review/futu-review.xlsx。")
    parser.add_argument("--node-bin", type=Path, default=DEFAULT_NODE_BIN)
    parser.add_argument("--skip-workbook", action="store_true", help="只生成 DB 和报告，不生成 Excel 审阅包。")
    parser.add_argument("--replace-db", action="store_true", help="目标 SQLite 已存在时覆盖。")
    parser.add_argument("--append-db", action="store_true", help="目标 SQLite 已存在时追加一个新的 import_run。")
    parser.add_argument("--strict", action="store_true", help="parser 验收失败或存在待复核问题时返回非 0。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    pdf_dir = args.pdf_dir.resolve()
    schema_path = args.schema_path.resolve()
    management_schema_path = None if args.skip_management_schema else args.management_schema_path.resolve()
    work_dir = (args.work_dir or (WORKSPACE_ROOT / "cache" / "futu-ingest-runs" / args.run_id)).resolve()
    cache_dir = work_dir / "pdf-cache"
    parser_out_dir = work_dir / "parser-v1"
    report_dir = work_dir / "reports"
    db_path = (args.db_path or (work_dir / "futu_raw_fact.sqlite")).resolve()
    review_dir = work_dir / "review"
    review_xlsx = None if args.skip_workbook else (args.review_xlsx or (review_dir / "futu-review.xlsx")).resolve()
    node_bin = args.node_bin.resolve() if args.node_bin else None

    cache_manifest = extract_pdf_cache(pdf_dir, args.glob, cache_dir)
    acceptance_report = run_parser(cache_dir, parser_out_dir)
    if args.append_db:
        resolve_prior_fund_amount_only_links(db_path, parser_out_dir)
    db_counts = load_sqlite(
        db_path=db_path,
        schema_path=schema_path,
        import_run_id=args.run_id,
        pdf_dir=pdf_dir,
        pdf_glob=args.glob,
        cache_dir=cache_dir,
        parser_out_dir=parser_out_dir,
        review_xlsx=review_xlsx,
        acceptance_report=acceptance_report,
        replace_db=args.replace_db,
        append_db=args.append_db,
        management_schema_path=management_schema_path,
    )
    if review_xlsx is not None:
        build_review_workbook(
            parser_out_dir=parser_out_dir,
            output_dir=review_dir,
            review_xlsx=review_xlsx,
            node_bin=node_bin,
            title="富途结单标准事实表审阅包",
            scope=f"{cache_manifest.get('statement_count', 0)} 份富途月结单",
        )
    report = write_ingest_reports(
        report_dir=report_dir,
        import_run_id=args.run_id,
        cache_manifest=cache_manifest,
        acceptance_report=acceptance_report,
        db_counts=db_counts,
        db_path=db_path,
        review_xlsx=review_xlsx,
        parser_out_dir=parser_out_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and report["status"] != "passed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
