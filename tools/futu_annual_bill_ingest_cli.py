#!/usr/bin/env python3
"""Futu official annual bill ingestion CLI.

This importer is a fast historical backfill path. It parses Futu's official
annual xlsx and writes the same raw fact tables used by monthly PDF ingestion,
while marking annual-only limitations as non-blocking parser issues.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "futu_raw_fact_schema_v1.sql"
DEFAULT_MANAGEMENT_SCHEMA = WORKSPACE_ROOT / "schema" / "investment_management_schema_v1.sql"
PARSER_MODULE_PATH = SCRIPT_DIR / "futu_statement_parser_v1.py"
PARSER_VERSION = "futu_annual_bill_parser_v1"
EXTRACTOR_VERSION = "futu_annual_bill_xlsx_extractor_v1"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

FUND_CODE_TO_PDF = {
    "880002": "HK0000478930",
    "880022": "HK0000584752",
    "880004": "HK0000499787",
}

FUND_NAME_BY_CODE = {
    "HK0000478930": "高騰微財貨幣基金",
    "HK0000584752": "高騰微金美元貨幣基金",
    "HK0000499787": "易方達港元貨幣市場基金",
}

RAW_TYPE_MAP = {
    "港股IPO公开发售": "港股IPO公開發售",
    "公司行动": "公司行動",
    "证券月度利息扣除": "證券月度利息扣除",
    "股票收益计划": "股票收益計劃",
    "期权行权": "期權行權",
    "基金申购": "基金申購",
    "基金赎回": "基金贖回",
}

TRADE_DIRECTION_MAP = {
    "卖出平仓": "賣出平倉",
    "卖出开仓": "賣出開倉",
    "买入平仓": "買入平倉",
    "买入开仓": "買入開倉",
}

CACHE_TABLES = ["raw_statements", "position_snapshots", "statement_balance_snapshots"]
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

CACHE_FIELDS: dict[str, list[str]] = {
    "annual_accounts": [
        "statement_id",
        "account_id",
        "owner_label",
        "platform",
        "broker",
        "account_label",
        "base_currency",
        "status",
        "notes",
    ],
    "raw_statements": ["statement_id", "period", "filename", "sha256", "pages", "source_file"],
    "position_snapshots": [
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
    "statement_balance_snapshots": [
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
}

PARSER_FIELDS: dict[str, list[str]] = {
    "cash_ledger_entries": [
        "cash_entry_id",
        "statement_id",
        "period",
        "filename",
        "page",
        "event_date",
        "business_type",
        "cash_leg_type",
        "direction_raw",
        "event_type_raw",
        "currency",
        "amount",
        "description",
        "raw_line",
        "source_refs",
        "dedupe_status",
        "source_count",
        "mapping_status",
    ],
    "market_trades": [
        "trade_id",
        "statement_id",
        "period",
        "filename",
        "page",
        "business_type",
        "trade_datetime",
        "trade_date",
        "settlement_date",
        "raw_direction",
        "side",
        "position_effect",
        "market",
        "currency",
        "instrument_code_raw",
        "instrument_symbol",
        "instrument_name_raw",
        "instrument_type",
        "underlying_symbol",
        "expiry_date",
        "strike_price",
        "option_type",
        "quantity",
        "quantity_unit",
        "price",
        "gross_amount",
        "fee_total",
        "net_cash_amount",
        "source_refs",
    ],
    "market_trade_fee_items": [
        "fee_tax_item_id",
        "trade_index",
        "parent_event_id",
        "parent_business_type",
        "statement_id",
        "period",
        "fee_tax_type",
        "raw_label",
        "currency",
        "amount_abs",
        "source_ref",
    ],
    "fund_orders": [
        "fund_order_id",
        "statement_id",
        "period",
        "fund_order_type",
        "instrument_code",
        "instrument_name_raw",
        "currency",
        "order_date",
        "trade_date",
        "quantity",
        "price",
        "fund_amount_abs",
        "evidence_status",
        "cash_match_status",
        "cash_match_source_refs",
        "evidence_row_ids",
        "source_refs",
    ],
    "fund_order_cash_legs": [
        "fund_cash_leg_id",
        "cash_entry_id",
        "fund_order_id",
        "statement_id",
        "event_date",
        "cash_leg_type",
        "currency",
        "cash_amount",
        "match_status",
        "candidate_order_ids",
    ],
    "fund_transactions": [
        "fund_transaction_row_id",
        "statement_id",
        "period",
        "filename",
        "page",
        "source_ref",
        "transaction_type_raw",
        "fund_order_type",
        "instrument_code",
        "instrument_name_raw",
        "currency",
        "order_date",
        "trade_date",
        "quantity",
        "price",
        "amount_abs",
        "evidence_status",
        "raw_line",
    ],
    "fund_transaction_fee_lines": [
        "fund_fee_row_id",
        "fund_transaction_row_id",
        "statement_id",
        "period",
        "filename",
        "page",
        "source_ref",
        "fee_amount",
        "subtotal",
        "raw_line",
        "mapping_status",
    ],
    "corporate_action_cash_legs": [
        "corporate_action_cash_leg_id",
        "cash_entry_id",
        "statement_id",
        "period",
        "event_date",
        "instrument_code_raw",
        "instrument_mapping_status",
        "corporate_action_group_type",
        "corporate_action_type",
        "currency",
        "cash_amount",
        "quantity_basis",
        "rate_raw",
        "description_raw",
        "source_refs",
        "dedupe_status",
    ],
    "asset_movement_events": [
        "asset_movement_id",
        "statement_id",
        "period",
        "filename",
        "page",
        "source_ref",
        "business_type",
        "asset_movement_type",
        "event_date",
        "direction_raw",
        "event_type_raw",
        "instrument_code_raw",
        "currency",
        "quantity",
        "amount",
        "description_raw",
    ],
    "derivative_exercise_events": [
        "derivative_exercise_id",
        "statement_id",
        "period",
        "event_date",
        "exercise_type",
        "option_instrument_raw",
        "cash_entry_id",
        "cash_amount",
        "asset_movement_ids",
        "source_refs",
        "description_raw",
    ],
    "financing_interest_events": [
        "financing_interest_event_id",
        "cash_entry_id",
        "statement_id",
        "period",
        "cash_event_date",
        "interest_type",
        "period_label",
        "currency",
        "cash_amount",
        "source_refs",
    ],
    "financing_interest_evidence_items": [
        "financing_interest_evidence_id",
        "statement_id",
        "period",
        "evidence_date",
        "currency",
        "financing_amount",
        "annual_rate_raw",
        "daily_interest",
        "cumulative_interest",
        "source_ref",
        "raw_line",
    ],
    "stock_yield_daily_events": [
        "stock_yield_daily_id",
        "statement_id",
        "period",
        "source_ref",
        "event_date",
        "instrument_code_raw",
        "market",
        "currency",
        "interest_type_raw",
        "quantity",
        "settlement_amount",
        "collateral_amount",
        "annual_rate_raw",
        "interest_amount",
        "cumulative_interest",
        "income_month",
    ],
    "stock_yield_cash_entries": [
        "stock_yield_cash_id",
        "cash_entry_id",
        "statement_id",
        "period",
        "event_date",
        "currency",
        "cash_amount",
        "description_raw",
        "income_month_guess",
        "reconciliation_status",
        "source_refs",
    ],
    "parser_issues": ["issue_id", "statement_id", "source_ref", "issue_type", "severity", "status", "message"],
    "unclassified_governance": [
        "unclassified_governance_id",
        "statement_id",
        "period",
        "source_ref",
        "raw_row",
        "default_classification",
        "status",
    ],
}


@dataclass
class AnnualParseResult:
    cache: dict[str, list[dict[str, Any]]]
    outputs: dict[str, list[dict[str, Any]]]
    acceptance_report: dict[str, Any]
    manifest: dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_run_id() -> str:
    return datetime.now().strftime("futu_annual_ingest_%Y%m%d_%H%M%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cell_to_pos(ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)(\d+)", ref)
    if not match:
        raise ValueError(f"Bad cell ref: {ref}")
    letters, row = match.groups()
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return int(row), col


def parse_cell(cell: ET.Element) -> str:
    inline = cell.find("m:is", NS)
    if inline is not None:
        return "".join(t.text or "" for t in inline.findall(".//m:t", NS))
    value = cell.find("m:v", NS)
    return value.text if value is not None and value.text is not None else ""


def workbook_sheet_map(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_by_id = {
        rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
        for rel in rels.findall("rel:Relationship", NS)
    }
    out: list[tuple[str, str]] = []
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{{{NS['r']}}}id"]
        target = rel_by_id[rel_id]
        if not target.startswith("xl/"):
            target = "xl/" + target
        out.append((name, target))
    return out


def parse_sheet(zf: zipfile.ZipFile, xml_path: str) -> list[dict[str, str]]:
    root = ET.fromstring(zf.read(xml_path))
    cells: dict[tuple[int, int], str] = {}
    max_row = 0
    max_col = 0
    for cell in root.findall(".//m:c", NS):
        ref = cell.attrib.get("r")
        if not ref:
            continue
        row, col = cell_to_pos(ref)
        text = parse_cell(cell)
        cells[(row, col)] = text
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    if max_row == 0:
        return []
    header = [cells.get((1, col), "") for col in range(1, max_col + 1)]
    rows: list[dict[str, str]] = []
    for row_i in range(2, max_row + 1):
        row = {header[col - 1]: cells.get((row_i, col), "") for col in range(1, max_col + 1)}
        if any(v not in ("", None) for v in row.values()):
            row["_annual_row_index"] = str(row_i)
            rows.append(row)
    return rows


def parse_workbook(path: Path) -> dict[str, list[dict[str, str]]]:
    with zipfile.ZipFile(path) as zf:
        return {name: parse_sheet(zf, xml_path) for name, xml_path in workbook_sheet_map(zf)}


def dec(value: Any, places: int | None = None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value).replace(",", "").replace("+", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if places is not None:
        q = Decimal(1).scaleb(-places)
        d = d.quantize(q, rounding=ROUND_HALF_UP)
    return d


def money(value: Any) -> str:
    d = dec(value, 2)
    return "" if d is None else format(d, ".2f")


def decimal_text(value: Any, places: int | None = None) -> str:
    d = dec(value, places)
    if d is None:
        return ""
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value).replace("\n", " ")).strip()


def normalize_raw_type(value: Any) -> str:
    text = normalize_space(value)
    return RAW_TYPE_MAP.get(text, text)


def event_direction(value: Any, amount: Any = None) -> str:
    text = normalize_space(value)
    if text == "In":
        return "增加"
    if text == "Out":
        return "減少"
    amount_d = dec(amount, 2)
    if amount_d is not None:
        return "增加" if amount_d >= 0 else "減少"
    return text


def trade_direction(value: Any) -> str:
    text = normalize_space(value)
    return TRADE_DIRECTION_MAP.get(text, text)


def date_slash(value: Any) -> str:
    text = normalize_space(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}/{text[4:6]}/{text[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10].replace("-", "/")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}.*", text):
        return text[:10]
    return "" if text == "-" else text


def datetime_slash(value: Any) -> str:
    text = normalize_space(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return text.replace("-", "/")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", text):
        return text
    return text


def date_from_datetime(value: Any) -> str:
    text = datetime_slash(value)
    return text[:10] if re.match(r"\d{4}/\d{2}/\d{2}", text) else date_slash(value)


def year_from_tables(path: Path, tables: dict[str, list[dict[str, str]]]) -> str:
    for row in tables.get("账户信息", []):
        if normalize_space(row.get("年份")):
            return normalize_space(row.get("年份"))
    match = re.search(r"20\d{2}", path.name)
    if match:
        return match.group(0)
    for sheet in ["证券-交易流水", "证券-资金进出", "证券-持仓总览"]:
        for row in tables.get(sheet, []):
            for field in ["成交时间", "日期"]:
                date = normalize_space(row.get(field))
                if re.match(r"20\d{2}", date):
                    return date[:4]
    raise ValueError(f"无法识别年度账单年份: {path}")


def source_ref(sheet: str, row: dict[str, Any]) -> str:
    return f"annual:{sheet}:row:{row.get('_annual_row_index', '')}"


def stable_account_hash(account_number: str) -> str:
    if not account_number:
        return "unknown"
    return hashlib.sha1(account_number.encode("utf-8")).hexdigest()[:10]


def annual_account_id(account_number: str, account_name: str = "") -> str:
    name = normalize_space(account_name)
    primary_account_number = os.environ.get("FUTU_PRIMARY_ACCOUNT_NUMBER", "")
    if primary_account_number and account_number == primary_account_number and ("證券" in name or "证券" in name):
        return "futu_hk_main"
    return f"futu_acct_{stable_account_hash(account_number)}"


def statement_id_for(year: str, account_number: str) -> str:
    return f"{year}_annual_acct_{stable_account_hash(account_number)}"


def collect_account_names(tables: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for sheet in ["账户信息", "证券-交易流水", "证券-资金进出", "证券-资产进出", "证券-持仓总览", "证券-资金总览"]:
        for row in tables.get(sheet, []):
            account = normalize_space(row.get("账户号码"))
            name = normalize_space(row.get("账户名称"))
            if account and name and account not in names:
                names[account] = name
    return names


def account_base_currency(account_name: str) -> str:
    name = normalize_space(account_name)
    if "美元" in name or "美股" in name:
        return "USD"
    return "HKD"


def normalize_fund_code(value: Any) -> str:
    code = normalize_space(value)
    return FUND_CODE_TO_PDF.get(code, code)


def normalize_security_symbol(value: Any, market: Any = None) -> str:
    code = normalize_space(value)
    if code in FUND_CODE_TO_PDF:
        return FUND_CODE_TO_PDF[code]
    if code.isdigit() and normalize_space(market) in {"SEHK", "FUTU OTC", ""}:
        return code.zfill(5)
    return code


def load_pdf_parser() -> Any:
    spec = importlib.util.spec_from_file_location("futu_statement_parser_v1", PARSER_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 parser: {PARSER_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def add_issue(
    issues: list[dict[str, Any]],
    issue_type: str,
    severity: str,
    status: str,
    message: str,
    statement_id: str = "",
    source: str = "",
) -> None:
    issues.append(
        {
            "issue_id": f"issue_{len(issues) + 1:04d}",
            "statement_id": statement_id,
            "source_ref": source,
            "issue_type": issue_type,
            "severity": severity,
            "status": status,
            "message": message,
        }
    )


def annual_instrument(row: dict[str, Any], parser_module: Any) -> dict[str, Any]:
    category = normalize_space(row.get("品类"))
    raw_code = normalize_space(row.get("代码名称"))
    market = normalize_space(row.get("交易所/市场"))
    if category == "期权":
        parsed = parser_module.normalize_instrument_from_fragments([raw_code])
        if parsed.get("instrument_type") == "option":
            parsed["instrument_code_raw"] = raw_code
            return parsed
    if category == "证券":
        return {
            "instrument_code_raw": raw_code,
            "instrument_symbol": normalize_security_symbol(raw_code, market),
            "instrument_name_raw": "",
            "instrument_type": "stock",
            "underlying_symbol": "",
            "expiry_date": "",
            "strike_price": "",
            "option_type": "",
            "quantity_unit": "share",
        }
    return {
        "instrument_code_raw": raw_code,
        "instrument_symbol": normalize_security_symbol(raw_code, market),
        "instrument_name_raw": "",
        "instrument_type": "unknown",
        "underlying_symbol": "",
        "expiry_date": "",
        "strike_price": "",
        "option_type": "",
        "quantity_unit": "",
    }


def build_raw_statements(
    xlsx: Path,
    year: str,
    account_numbers: set[str],
    account_filter: set[str] | None,
) -> list[dict[str, Any]]:
    selected = sorted(account_numbers if account_filter is None else account_numbers & account_filter)
    if not selected and account_filter:
        selected = sorted(account_filter)
    file_hash = sha256_file(xlsx)
    return [
        {
            "statement_id": statement_id_for(year, account),
            "period": year,
            "filename": xlsx.name,
            "sha256": file_hash,
            "pages": 0,
            "source_file": str(xlsx),
        }
        for account in selected
    ]


def build_annual_accounts(
    year: str,
    account_numbers: set[str],
    account_filter: set[str] | None,
    account_names: dict[str, str],
) -> list[dict[str, Any]]:
    selected = sorted(account_numbers if account_filter is None else account_numbers & account_filter)
    rows: list[dict[str, Any]] = []
    for account in selected:
        account_name = account_names.get(account, "")
        account_id = annual_account_id(account, account_name)
        label = account_name or f"富途年度账单账户 {stable_account_hash(account)}"
        rows.append(
            {
                "statement_id": statement_id_for(year, account),
                "account_id": account_id,
                "owner_label": "personal",
                "platform": "futu",
                "broker": "富途",
                "account_label": label,
                "base_currency": account_base_currency(account_name),
                "status": "active",
                "notes": f"由 {year} 富途年度账单生成；account_id 使用脱敏稳定哈希。",
            }
        )
    return rows


def build_position_snapshots(
    tables: dict[str, list[dict[str, str]]],
    xlsx: Path,
    year: str,
    account_filter: set[str] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in tables.get("证券-持仓总览", []):
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        snapshot_type = "opening" if normalize_space(row.get("时期类型")) == "期初" else "closing"
        category = normalize_space(row.get("品类"))
        asset_category = "fund" if category == "基金" else "stock_or_option"
        market = normalize_space(row.get("交易所/市场"))
        code = normalize_space(row.get("代码名称"))
        if category == "基金":
            code_name = normalize_fund_code(code)
        elif category == "证券":
            code_name = normalize_security_symbol(code, market)
        else:
            code_name = code
        section = f"annual_{snapshot_type}_{asset_category}_positions"
        rows.append(
            {
                "statement_id": statement_id_for(year, account),
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "table_index": 4,
                "row_index": row.get("_annual_row_index", ""),
                "section": section,
                "snapshot_type": snapshot_type,
                "asset_category": asset_category,
                "code_name": code_name,
                "market": market,
                "currency": normalize_space(row.get("币种")),
                "quantity": decimal_text(row.get("数量/面值"), 8),
                "price": decimal_text(row.get("价格"), 8),
                "multiplier": decimal_text(row.get("乘数"), 8),
                "market_value": money(row.get("市值")),
                "price_date": date_slash(row.get("日期")),
                "pending_amount": "",
                "initial_margin_requirement": "",
                "maintenance_margin_requirement": "",
                "maintenance_margin_rate": "",
            }
        )
    return rows


def build_statement_balances(
    tables: dict[str, list[dict[str, str]]],
    year: str,
    account_filter: set[str] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in tables.get("证券-资金总览", []):
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        snapshot_type = "opening" if normalize_space(row.get("时期类型")) == "期初" else "closing"
        rows.append(
            {
                "balance_snapshot_id": f"annual_bal_{len(rows) + 1:04d}",
                "account_id": "futu_hk_main",
                "statement_id": statement_id_for(year, account),
                "period": year,
                "snapshot_type": snapshot_type,
                "currency": normalize_space(row.get("币种")),
                "reported_balance": money(row.get("金额")),
                "source_ref": source_ref("证券-资金总览", row),
                "status": "active",
                "notes": f"年度账单资金总览；日期={date_slash(row.get('日期'))}",
            }
        )
    return rows


def build_cash_entries(
    tables: dict[str, list[dict[str, str]]],
    xlsx: Path,
    year: str,
    account_filter: set[str] | None,
    parser_module: Any,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    fund_cash_by_source: dict[str, str] = {}

    def append_cash(
        statement_id: str,
        event_date: str,
        direction_raw: str,
        event_type_raw: str,
        currency: str,
        amount_value: Any,
        description: str,
        ref: str,
        raw_line: str,
        mapping_status: str,
    ) -> str:
        amount_text = money(amount_value)
        business_type, cash_leg_type = parser_module.classify_cash(
            event_type_raw,
            direction_raw,
            float(amount_text or 0),
            description,
        )
        cash_entry_id = f"cash_{len(rows) + 1:04d}"
        rows.append(
            {
                "cash_entry_id": cash_entry_id,
                "statement_id": statement_id,
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "event_date": event_date,
                "business_type": business_type,
                "cash_leg_type": cash_leg_type,
                "direction_raw": direction_raw,
                "event_type_raw": event_type_raw,
                "currency": currency,
                "amount": amount_text,
                "description": description,
                "raw_line": raw_line,
                "source_refs": ref,
                "dedupe_status": "unique",
                "source_count": 1,
                "mapping_status": mapping_status,
            }
        )
        return cash_entry_id

    for row in tables.get("证券-资金进出", []):
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        event_type_raw = normalize_raw_type(row.get("类型"))
        append_cash(
            statement_id_for(year, account),
            date_slash(row.get("日期")),
            event_direction(row.get("方向"), row.get("变动金额")),
            event_type_raw,
            normalize_space(row.get("币种")),
            row.get("变动金额"),
            normalize_space(row.get("备注")),
            source_ref("证券-资金进出", row),
            json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, ensure_ascii=False, sort_keys=True),
            "mapped_from_annual_cash",
        )

    for row in tables.get("证券-交易流水", []):
        if normalize_space(row.get("品类")) != "基金":
            continue
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        direction = normalize_space(row.get("方向"))
        event_type_raw = "基金申購" if direction == "申购" else "基金贖回" if direction == "赎回" else direction
        amount_value = row.get("变动金额")
        ref = source_ref("证券-交易流水", row)
        cash_id = append_cash(
            statement_id_for(year, account),
            date_from_datetime(row.get("成交时间")),
            event_direction("", amount_value),
            event_type_raw,
            normalize_space(row.get("币种")),
            amount_value,
            f"Annual bill fund trade cash effect: {normalize_fund_code(row.get('代码名称'))}",
            ref,
            json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, ensure_ascii=False, sort_keys=True),
            "derived_from_annual_trade",
        )
        fund_cash_by_source[ref] = cash_id

    return rows, fund_cash_by_source


def build_market_trades(
    tables: dict[str, list[dict[str, str]]],
    xlsx: Path,
    year: str,
    account_filter: set[str] | None,
    parser_module: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    fee_items: list[dict[str, Any]] = []
    for row in tables.get("证券-交易流水", []):
        category = normalize_space(row.get("品类"))
        if category == "基金":
            continue
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        raw_direction = trade_direction(row.get("方向"))
        side = "sell" if raw_direction.startswith("賣出") else "buy" if raw_direction.startswith("買入") else "unknown"
        position_effect = "open" if "開倉" in raw_direction else "close" if "平倉" in raw_direction else "unknown"
        trade_id = f"mt_{len(trades) + 1:04d}"
        instrument = annual_instrument(row, parser_module)
        fee_total = abs(dec(row.get("总费用"), 2) or Decimal("0"))
        trade = {
            "trade_id": trade_id,
            "statement_id": statement_id_for(year, account),
            "period": year,
            "filename": xlsx.name,
            "page": 0,
            "business_type": "market_trade",
            "trade_datetime": datetime_slash(row.get("成交时间")),
            "trade_date": date_from_datetime(row.get("成交时间")),
            "settlement_date": date_slash(row.get("交收日期")),
            "raw_direction": raw_direction,
            "side": side,
            "position_effect": position_effect,
            "market": normalize_space(row.get("交易所/市场")),
            "currency": normalize_space(row.get("币种")),
            "quantity": decimal_text(abs(dec(row.get("数量/面值"), 8) or Decimal("0")), 8),
            "price": decimal_text(row.get("价格"), 8),
            "gross_amount": money(abs(dec(row.get("成交金额"), 2) or Decimal("0"))),
            "fee_total": money(fee_total),
            "net_cash_amount": money(row.get("变动金额")),
            "source_refs": source_ref("证券-交易流水", row),
        }
        trade.update(instrument)
        trades.append(trade)
        if fee_total > 0:
            fee_items.append(
                {
                    "fee_tax_item_id": f"trade_fee_{len(fee_items) + 1:04d}",
                    "trade_index": trade_id,
                    "parent_event_id": trade_id,
                    "parent_business_type": "market_trade",
                    "statement_id": trade["statement_id"],
                    "period": year,
                    "fee_tax_type": "unknown_fee",
                    "raw_label": "年度账单总费用",
                    "currency": trade["currency"],
                    "amount_abs": money(fee_total),
                    "source_ref": trade["source_refs"],
                }
            )
    return trades, fee_items


def build_fund_tables(
    tables: dict[str, list[dict[str, str]]],
    xlsx: Path,
    year: str,
    account_filter: set[str] | None,
    fund_cash_by_source: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    fund_orders: list[dict[str, Any]] = []
    fund_cash_legs: list[dict[str, Any]] = []
    fund_transactions: list[dict[str, Any]] = []
    fund_fee_lines: list[dict[str, Any]] = []
    for row in tables.get("证券-交易流水", []):
        if normalize_space(row.get("品类")) != "基金":
            continue
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        ref = source_ref("证券-交易流水", row)
        statement_id = statement_id_for(year, account)
        direction = normalize_space(row.get("方向"))
        order_type = "subscription" if direction == "申购" else "redemption" if direction == "赎回" else direction
        fund_code = normalize_fund_code(row.get("代码名称"))
        fund_name = FUND_NAME_BY_CODE.get(fund_code, "")
        trade_date = date_from_datetime(row.get("成交时间"))
        amount_abs = money(abs(dec(row.get("变动金额"), 2) or Decimal("0")))
        quantity = decimal_text(abs(dec(row.get("数量/面值"), 8) or Decimal("0")), 8)
        tx_id = f"fund_tx_{len(fund_transactions) + 1:04d}"
        order_id = f"fund_order_{len(fund_orders) + 1:04d}"
        raw_line = json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, ensure_ascii=False, sort_keys=True)
        fund_transactions.append(
            {
                "fund_transaction_row_id": tx_id,
                "statement_id": statement_id,
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "source_ref": ref,
                "transaction_type_raw": "申購" if order_type == "subscription" else "贖回" if order_type == "redemption" else direction,
                "fund_order_type": order_type,
                "instrument_code": fund_code,
                "instrument_name_raw": fund_name,
                "currency": normalize_space(row.get("币种")),
                "order_date": trade_date,
                "trade_date": trade_date,
                "quantity": quantity,
                "price": decimal_text(row.get("价格"), 8),
                "amount_abs": amount_abs,
                "evidence_status": "complete_detail",
                "raw_line": raw_line,
            }
        )
        fund_orders.append(
            {
                "fund_order_id": order_id,
                "statement_id": statement_id,
                "period": year,
                "fund_order_type": order_type,
                "instrument_code": fund_code,
                "instrument_name_raw": fund_name,
                "currency": normalize_space(row.get("币种")),
                "order_date": trade_date,
                "trade_date": trade_date,
                "quantity": quantity,
                "price": decimal_text(row.get("价格"), 8),
                "fund_amount_abs": amount_abs,
                "evidence_status": "complete_detail",
                "cash_match_status": "matched_annual_cash_entry",
                "cash_match_source_refs": ref,
                "evidence_row_ids": tx_id,
                "source_refs": ref,
            }
        )
        cash_id = fund_cash_by_source.get(ref, "")
        cash_amount = money(row.get("变动金额"))
        fund_cash_legs.append(
            {
                "fund_cash_leg_id": f"fund_cash_{len(fund_cash_legs) + 1:04d}",
                "cash_entry_id": cash_id,
                "fund_order_id": order_id,
                "statement_id": statement_id,
                "event_date": trade_date,
                "cash_leg_type": "subscription_cash_out" if order_type == "subscription" else "redemption_cash_in",
                "currency": normalize_space(row.get("币种")),
                "cash_amount": cash_amount,
                "match_status": "matched_annual_cash_entry",
                "candidate_order_ids": order_id,
            }
        )
        fee = money(abs(dec(row.get("总费用"), 2) or Decimal("0")))
        fund_fee_lines.append(
            {
                "fund_fee_row_id": f"fund_fee_{len(fund_fee_lines) + 1:04d}",
                "fund_transaction_row_id": tx_id,
                "statement_id": statement_id,
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "source_ref": ref,
                "fee_amount": fee,
                "subtotal": fee,
                "raw_line": raw_line,
                "mapping_status": "annual_trade_fee_total",
            }
        )
    return fund_orders, fund_cash_legs, fund_transactions, fund_fee_lines


def build_asset_movements(
    tables: dict[str, list[dict[str, str]]],
    xlsx: Path,
    year: str,
    account_filter: set[str] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in tables.get("证券-资产进出", []):
        account = normalize_space(row.get("账户号码"))
        if account_filter and account not in account_filter:
            continue
        raw_type = normalize_raw_type(row.get("类型"))
        business_type = "ipo_subscription" if "IPO" in raw_type else "derivative_exercise" if "期權" in raw_type else "asset_movement"
        movement_type = "allotment" if business_type == "ipo_subscription" else "option_expiry_close" if business_type == "derivative_exercise" else "other"
        direction = event_direction(row.get("方向"))
        quantity = abs(dec(row.get("数量"), 8) or Decimal("0"))
        if direction == "減少":
            quantity = -quantity
        market = normalize_space(row.get("交易所/市场"))
        code = normalize_space(row.get("代码名称"))
        if normalize_space(row.get("品类")) == "证券":
            code = normalize_security_symbol(code, market)
        rows.append(
            {
                "asset_movement_id": f"asset_move_{len(rows) + 1:04d}",
                "statement_id": statement_id_for(year, account),
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "source_ref": source_ref("证券-资产进出", row),
                "business_type": business_type,
                "asset_movement_type": movement_type,
                "event_date": date_slash(row.get("日期")),
                "direction_raw": direction,
                "event_type_raw": raw_type,
                "instrument_code_raw": code,
                "currency": normalize_space(row.get("币种")),
                "quantity": decimal_text(quantity, 8),
                "amount": "",
                "description_raw": normalize_space(row.get("备注")),
            }
        )
    return rows


def income_month_from_annual_description(description: str, event_date: str) -> str:
    month_names = {
        "January": "01",
        "February": "02",
        "March": "03",
        "April": "04",
        "May": "05",
        "June": "06",
        "July": "07",
        "August": "08",
        "September": "09",
        "October": "10",
        "November": "11",
        "December": "12",
    }
    for name, month in month_names.items():
        if name in description:
            year = event_date[:4] if re.match(r"20\d{2}", event_date) else ""
            return f"{year}/{month}" if year else month
    return ""


def build_stock_yield_cash_entries(cash_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cash in cash_entries:
        if cash.get("business_type") != "securities_lending_income":
            continue
        rows.append(
            {
                "stock_yield_cash_id": f"stock_yield_cash_{len(rows) + 1:04d}",
                "cash_entry_id": cash["cash_entry_id"],
                "statement_id": cash["statement_id"],
                "period": cash["period"],
                "event_date": cash["event_date"],
                "currency": cash["currency"],
                "cash_amount": cash["amount"],
                "description_raw": cash["description"],
                "income_month_guess": income_month_from_annual_description(cash["description"], cash["event_date"]),
                "reconciliation_status": "annual_cash_only",
                "source_refs": cash["source_refs"],
            }
        )
    return rows


def append_synthetic_derivative_cash_entries(
    cash_entries: list[dict[str, Any]],
    asset_movements: list[dict[str, Any]],
    xlsx: Path,
    year: str,
    parser_module: Any,
) -> int:
    existing_keys = {
        (row.get("statement_id"), row.get("event_date"), row.get("description"))
        for row in cash_entries
        if row.get("business_type") == "derivative_exercise"
    }
    added = 0
    for asset in asset_movements:
        if asset.get("business_type") != "derivative_exercise":
            continue
        description = asset.get("description_raw") or f"Annual zero cash leg for {asset.get('instrument_code_raw', '')}"
        key = (asset.get("statement_id"), asset.get("event_date"), description)
        if key in existing_keys:
            continue
        business_type, cash_leg_type = parser_module.classify_cash("期權行權", "減少", 0.0, description)
        cash_entries.append(
            {
                "cash_entry_id": f"cash_{len(cash_entries) + 1:04d}",
                "statement_id": asset["statement_id"],
                "period": year,
                "filename": xlsx.name,
                "page": 0,
                "event_date": asset["event_date"],
                "business_type": business_type,
                "cash_leg_type": cash_leg_type,
                "direction_raw": "減少",
                "event_type_raw": "期權行權",
                "currency": asset.get("currency", ""),
                "amount": "0.00",
                "description": description,
                "raw_line": f"synthetic annual zero cash leg from {asset.get('source_ref', '')}",
                "source_refs": asset.get("source_ref", ""),
                "dedupe_status": "unique",
                "source_count": 1,
                "mapping_status": "synthetic_from_annual_asset_movement",
            }
        )
        added += 1
    return added


def build_acceptance_report(
    cache: dict[str, list[dict[str, Any]]],
    outputs: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def add(code: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        tests.append({"code": code, "actual": actual, "expected": expected, "passed": actual == expected if passed is None else passed})

    market_errors = []
    for row in outputs["market_trades"]:
        gross = dec(row["gross_amount"], 2) or Decimal("0")
        fee = dec(row["fee_total"], 2) or Decimal("0")
        net = dec(row["net_cash_amount"], 2) or Decimal("0")
        expected = gross - fee if row.get("side") == "sell" else -(gross + fee) if row.get("side") == "buy" else net
        if abs(expected - net) > Decimal("0.01"):
            market_errors.append(row["trade_id"])

    fee_sum_by_trade: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in outputs["market_trade_fee_items"]:
        fee_sum_by_trade[row["parent_event_id"]] += dec(row["amount_abs"], 2) or Decimal("0")
    fee_errors = []
    for row in outputs["market_trades"]:
        fee_total = dec(row["fee_total"], 2) or Decimal("0")
        if abs(fee_sum_by_trade[row["trade_id"]] - fee_total) > Decimal("0.01"):
            fee_errors.append(row["trade_id"])

    add("ANN-RAW-STATEMENTS-001", len(cache["raw_statements"]), ">=1", len(cache["raw_statements"]) >= 1)
    add("ANN-CASH-001", len(outputs["cash_ledger_entries"]), ">=1", len(outputs["cash_ledger_entries"]) >= 1)
    add("ANN-MARKET-001", len(outputs["market_trades"]), ">=1", len(outputs["market_trades"]) >= 1)
    add("ANN-MARKET-ARITH-001", len(market_errors), 0)
    add("ANN-MARKET-FEE-001", len(fee_errors), 0)
    add("ANN-FUND-ORDER-001", len(outputs["fund_orders"]), ">=0", len(outputs["fund_orders"]) >= 0)
    add("ANN-ASSET-MOVE-001", len(outputs["asset_movement_events"]), ">=0", len(outputs["asset_movement_events"]) >= 0)
    add("ANN-POSITION-001", len(cache["position_snapshots"]), ">=0", len(cache["position_snapshots"]) >= 0)
    pending_issues = [
        row
        for row in outputs["parser_issues"]
        if row.get("severity") == "needs_review" or row.get("status") in {"open", "ambiguous", "unmatched"}
    ]
    add("ANN-PENDING-ISSUES-001", len(pending_issues), 0)

    return {
        "parser_version": PARSER_VERSION,
        "status": "passed" if all(test["passed"] for test in tests) else "failed",
        "tests": tests,
        "counts": {name: len(rows) for name, rows in outputs.items()},
        "cache_counts": {name: len(rows) for name, rows in cache.items()},
    }


def parse_annual_bill(xlsx: Path, account_filter: set[str] | None) -> AnnualParseResult:
    parser_module = load_pdf_parser()
    tables = parse_workbook(xlsx)
    year = year_from_tables(xlsx, tables)
    account_numbers = {
        normalize_space(row.get("账户号码"))
        for sheet in ["证券-交易流水", "证券-资金进出", "证券-资产进出", "证券-持仓总览", "证券-资金总览"]
        for row in tables.get(sheet, [])
        if normalize_space(row.get("账户号码"))
    }
    account_names = collect_account_names(tables)
    raw_statements = build_raw_statements(xlsx, year, account_numbers, account_filter)
    cache = {
        "annual_accounts": build_annual_accounts(year, account_numbers, account_filter, account_names),
        "raw_statements": raw_statements,
        "position_snapshots": build_position_snapshots(tables, xlsx, year, account_filter),
        "statement_balance_snapshots": build_statement_balances(tables, year, account_filter),
    }
    cash_entries, fund_cash_by_source = build_cash_entries(tables, xlsx, year, account_filter, parser_module)
    market_trades, market_trade_fee_items = build_market_trades(tables, xlsx, year, account_filter, parser_module)
    fund_orders, fund_cash_legs, fund_transactions, fund_fee_lines = build_fund_tables(
        tables, xlsx, year, account_filter, fund_cash_by_source
    )
    asset_movements = build_asset_movements(tables, xlsx, year, account_filter)
    synthetic_derivative_cash_count = append_synthetic_derivative_cash_entries(
        cash_entries,
        asset_movements,
        xlsx,
        year,
        parser_module,
    )

    parser_state = parser_module.ParserState(cache_dir=Path("__annual_cache_placeholder__"), out_dir=Path("__annual_out_placeholder__"))
    financing_events, _financing_evidence = parser_module.parse_financing_interest(parser_state, cash_entries)
    derivative_events = parser_module.parse_derivative_events(cash_entries, asset_movements)

    parser_issues: list[dict[str, Any]] = []
    add_issue(
        parser_issues,
        "annual_bill_granularity",
        "info",
        "documented",
        "年度账单为年度粒度来源；不会生成月度期初/期末连续余额、PDF 页码或日度融资利息证据。",
    )
    if asset_movements:
        add_issue(
            parser_issues,
            "annual_asset_movement_amount_absent",
            "info",
            "documented",
            "年度账单证券-资产进出不提供资产腿金额列；asset_movement_events.amount 保留为空。",
        )
    if market_trade_fee_items:
        add_issue(
            parser_issues,
            "annual_market_fee_components_unavailable",
            "info",
            "documented",
            "年度账单只披露交易总费用；market_trade_fee_items 使用年度账单总费用，不拆分佣金、印花税等组件。",
        )
    if fund_orders:
        add_issue(
            parser_issues,
            "annual_fund_cash_entry_synthetic",
            "info",
            "documented",
            "年度账单基金现金影响来自证券-交易流水，已生成与 PDF 口径一致的基金现金腿和派生 cash_ledger_entries。",
        )
    if synthetic_derivative_cash_count:
        add_issue(
            parser_issues,
            "annual_derivative_zero_cash_synthetic",
            "info",
            "documented",
            f"年度账单期权资产进出不列 0 金额现金腿；已按资产腿补充 {synthetic_derivative_cash_count} 条 0 金额 derivative cash leg。",
        )

    outputs = {
        "cash_ledger_entries": cash_entries,
        "market_trades": market_trades,
        "market_trade_fee_items": market_trade_fee_items,
        "fund_orders": fund_orders,
        "fund_order_cash_legs": fund_cash_legs,
        "fund_transactions": fund_transactions,
        "fund_transaction_fee_lines": fund_fee_lines,
        "corporate_action_cash_legs": parser_module.parse_corporate_actions(parser_state, cash_entries),
        "asset_movement_events": asset_movements,
        "derivative_exercise_events": derivative_events,
        "financing_interest_events": financing_events,
        "financing_interest_evidence_items": [],
        "stock_yield_daily_events": [],
        "stock_yield_cash_entries": build_stock_yield_cash_entries(cash_entries),
        "parser_issues": parser_issues,
        "unclassified_governance": [],
    }
    acceptance_report = build_acceptance_report(cache, outputs)
    manifest = {
        "extractor_version": EXTRACTOR_VERSION,
        "parser_version": PARSER_VERSION,
        "xlsx": str(xlsx),
        "year": year,
        "account_filter": sorted(account_filter or []),
        "statement_count": len(raw_statements),
        "sheets": {name: len(rows) for name, rows in tables.items()},
        "cache_counts": acceptance_report["cache_counts"],
        "parser_counts": acceptance_report["counts"],
        "status": acceptance_report["status"],
    }
    return AnnualParseResult(cache=cache, outputs=outputs, acceptance_report=acceptance_report, manifest=manifest)


def write_annual_outputs(result: AnnualParseResult, cache_dir: Path, parser_out_dir: Path) -> None:
    for name, fields in CACHE_FIELDS.items():
        write_csv(cache_dir / f"{name}.csv", result.cache.get(name, []), fields)
    for name, fields in PARSER_FIELDS.items():
        write_csv(parser_out_dir / f"{name}.csv", result.outputs.get(name, []), fields)
    cache_dir.mkdir(parents=True, exist_ok=True)
    parser_out_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "manifest.json").write_text(json.dumps(result.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (parser_out_dir / "acceptance_report.json").write_text(
        json.dumps(result.acceptance_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (parser_out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "parser_version": PARSER_VERSION,
                "source_cache": str(cache_dir),
                "outputs": sorted(f"{name}.csv" for name in result.outputs),
                "acceptance_report": "acceptance_report.json",
                "status": result.acceptance_report["status"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def sqlite_columns(conn: sqlite3.Connection, table_name: str) -> dict[str, str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1]: row[2].upper() for row in rows}


def sqlite_object_exists(conn: sqlite3.Connection, object_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (object_name,),
    ).fetchone()
    return row is not None


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


def apply_management_schema(conn: sqlite3.Connection, management_schema_path: Path | None) -> None:
    if management_schema_path is not None:
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
          'annual_bill_default_futu_hk_main',
          'inferred',
          '由富途年度账单导入批次默认挂接；不在管理层保存敏感账号。'
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


def insert_annual_accounts_and_links(conn: sqlite3.Connection, import_run_id: str, cache_dir: Path) -> tuple[int, int]:
    rows = read_csv(cache_dir / "annual_accounts.csv")
    if not rows or not sqlite_object_exists(conn, "accounts") or not sqlite_object_exists(conn, "statement_accounts"):
        return 0, 0
    before_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.executemany(
        """
        INSERT OR IGNORE INTO accounts (
          account_id, owner_label, platform, broker, account_label,
          base_currency, status, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.get("account_id", ""),
                row.get("owner_label", ""),
                row.get("platform", ""),
                row.get("broker", ""),
                row.get("account_label", ""),
                row.get("base_currency", ""),
                row.get("status", "active"),
                row.get("notes", ""),
            )
            for row in rows
        ],
    )
    after_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    before_links = conn.execute(
        "SELECT COUNT(*) FROM statement_accounts WHERE import_run_id = ?",
        (import_run_id,),
    ).fetchone()[0]
    conn.executemany(
        """
        INSERT OR IGNORE INTO statement_accounts (
          import_run_id, statement_id, account_id, link_source, confidence, notes
        )
        VALUES (?, ?, ?, 'annual_bill_account_mapping', 'inferred', ?)
        """,
        [
            (
                import_run_id,
                row.get("statement_id", ""),
                row.get("account_id", ""),
                "年度账单账户映射；account_id 为脱敏稳定键。",
            )
            for row in rows
        ],
    )
    after_links = conn.execute(
        "SELECT COUNT(*) FROM statement_accounts WHERE import_run_id = ?",
        (import_run_id,),
    ).fetchone()[0]
    return int(after_accounts - before_accounts), int(after_links - before_links)


def load_sqlite(
    db_path: Path,
    schema_path: Path,
    management_schema_path: Path | None,
    import_run_id: str,
    xlsx: Path,
    cache_dir: Path,
    parser_out_dir: Path,
    acceptance_report: dict[str, Any],
    replace_db: bool,
    append_db: bool,
) -> dict[str, int]:
    if replace_db and append_db:
        raise ValueError("--replace-db 与 --append-db 不能同时使用。")
    if db_path.exists():
        if replace_db:
            db_path.unlink()
        elif not append_db:
            raise FileExistsError(f"SQLite 已存在，请换路径或使用 --replace-db: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
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
                acceptance_report.get("parser_version", PARSER_VERSION),
                EXTRACTOR_VERSION,
                "loaded",
                str(xlsx.parent),
                xlsx.name,
                str(cache_dir),
                str(parser_out_dir),
                str(db_path),
                "",
                len(raw_statements),
                acceptance_report.get("status", "unknown"),
                "source_mode=annual_bill; raw fact layer only; annual granularity limitations documented in parser_issues",
            ),
        )
        counts: dict[str, int] = {}
        for table_name in CACHE_TABLES:
            counts[table_name] = insert_csv_table(conn, table_name, cache_dir / f"{table_name}.csv", import_run_id)
        for table_name in PARSER_TABLES:
            counts[table_name] = insert_csv_table(conn, table_name, parser_out_dir / f"{table_name}.csv", import_run_id)
        account_rows, statement_links = insert_annual_accounts_and_links(conn, import_run_id, cache_dir)
        if account_rows:
            counts["accounts"] = account_rows
        if not statement_links:
            statement_links = bootstrap_statement_accounts(conn, import_run_id)
        if statement_links:
            counts["statement_accounts"] = statement_links
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
    manifest: dict[str, Any],
    acceptance_report: dict[str, Any],
    db_counts: dict[str, int],
    db_path: Path,
    parser_out_dir: Path,
) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    pending = summarize_pending_issues(parser_out_dir)
    report = {
        "import_run_id": import_run_id,
        "created_at": utc_now(),
        "status": "passed" if acceptance_report.get("status") == "passed" and pending["pending_count"] == 0 else "needs_review",
        "source_mode": "annual_bill",
        "manifest": manifest,
        "acceptance_status": acceptance_report.get("status"),
        "parser_counts": acceptance_report.get("counts", {}),
        "cache_counts": acceptance_report.get("cache_counts", {}),
        "db_counts": db_counts,
        "pending_issues": pending,
        "db_path": str(db_path),
    }
    (report_dir / "ingest_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# 富途年度账单导入报告：{import_run_id}",
        "",
        f"- 导入状态：`{report['status']}`",
        f"- Parser 验收状态：`{report['acceptance_status']}`",
        f"- 来源模式：`annual_bill`",
        f"- 年份：`{manifest.get('year', '')}`",
        f"- 账单数：`{manifest.get('statement_count', 0)}`",
        f"- SQLite：`{db_path}`",
        f"- 待复核问题数：`{pending['pending_count']}`",
        "",
        "## 表行数",
        "",
        "| 表 | 行数 |",
        "| --- | ---: |",
    ]
    for table_name, row_count in sorted(db_counts.items()):
        lines.append(f"| `{table_name}` | {row_count} |")
    (report_dir / "ingest_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入富途官方年度账单 xlsx 到原始事实 SQLite。")
    parser.add_argument("--xlsx", required=True, type=Path, help="富途官方年度账单 xlsx。")
    parser.add_argument("--account-number", action="append", help="只导入指定账户号码；可重复。默认导入年度账单中有数据的证券账户。")
    parser.add_argument("--work-dir", type=Path, help="本次运行目录；默认写入 workspace cache/futu-annual-ingest-runs/<run_id>。")
    parser.add_argument("--run-id", default=local_run_id(), help="导入运行 ID。")
    parser.add_argument("--db-path", type=Path, help="SQLite 输出路径；默认 work-dir/futu_annual_raw_fact.sqlite。")
    parser.add_argument("--schema-path", default=DEFAULT_SCHEMA, type=Path, help="SQLite schema SQL 文件。")
    parser.add_argument(
        "--management-schema-path",
        default=DEFAULT_MANAGEMENT_SCHEMA,
        type=Path,
        help="投资数据库管理层 schema SQL 文件；默认自动应用。",
    )
    parser.add_argument("--skip-management-schema", action="store_true", help="只写入 raw fact schema，不应用管理层。")
    parser.add_argument("--replace-db", action="store_true", help="目标 SQLite 已存在时覆盖。")
    parser.add_argument("--append-db", action="store_true", help="目标 SQLite 已存在时追加为新的 import_run。")
    parser.add_argument("--strict", action="store_true", help="parser 验收失败或存在待复核问题时返回非 0。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    xlsx = args.xlsx.resolve()
    schema_path = args.schema_path.resolve()
    management_schema_path = None if args.skip_management_schema else args.management_schema_path.resolve()
    work_dir = (args.work_dir or (WORKSPACE_ROOT / "cache" / "futu-annual-ingest-runs" / args.run_id)).resolve()
    cache_dir = work_dir / "annual-cache"
    parser_out_dir = work_dir / "parser-v1"
    report_dir = work_dir / "reports"
    db_path = (args.db_path or (work_dir / "futu_annual_raw_fact.sqlite")).resolve()
    account_filter = set(args.account_number) if args.account_number else None

    result = parse_annual_bill(xlsx, account_filter)
    write_annual_outputs(result, cache_dir, parser_out_dir)
    db_counts = load_sqlite(
        db_path=db_path,
        schema_path=schema_path,
        management_schema_path=management_schema_path,
        import_run_id=args.run_id,
        xlsx=xlsx,
        cache_dir=cache_dir,
        parser_out_dir=parser_out_dir,
        acceptance_report=result.acceptance_report,
        replace_db=args.replace_db,
        append_db=args.append_db,
    )
    report = write_ingest_reports(
        report_dir=report_dir,
        import_run_id=args.run_id,
        manifest=result.manifest,
        acceptance_report=result.acceptance_report,
        db_counts=db_counts,
        db_path=db_path,
        parser_out_dir=parser_out_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and report["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
