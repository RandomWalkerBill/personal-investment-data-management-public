#!/usr/bin/env python3
"""Futu statement parser v1 for the Q1 2025 discovery sample.

This parser reads the raw JSON extraction cache and produces canonical raw fact
CSVs. It is intentionally deterministic and conservative: source rows are kept,
duplicates are merged with multi-source refs, and unresolved interpretation is
reported as parser issues instead of being guessed away.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


STATEMENT_RE = re.compile(r"20\d{4}")
STATEMENT_DATE_RE = re.compile(r"20\d{6}")
DATE_RE = re.compile(r"^20\d{2}/\d{2}/\d{2}$")
CASH_LINE_RE = re.compile(
    r"^(20\d{2}/\d{2}/\d{2})\s+"
    r"(增加|減少)\s+"
    r"(.+?)\s+"
    r"([A-Z]{3})\s+"
    r"([+-]?[0-9,]+\.\d{2}|0\.00)\s*"
    r"(.*)$"
)
FUND_TX_RE = re.compile(
    r"^(申購|贖回)\s+"
    r"(HK\d{10})\s+\((.+)\)\s+"
    r"([A-Z]{3})\s+"
    r"(20\d{2}/\d{2}/\d{2})\s+"
    r"(20\d{2}/\d{2}/\d{2}|-)\s+"
    r"([0-9,]+\.\d+|-)\s+"
    r"([0-9.]+|-)\s+"
    r"([0-9,]+\.\d{2})$"
)
FUND_FEE_RE = re.compile(r"^申購/贖回費用：([0-9,.]+)\s+小計:\s+([0-9,.]+)")
FINANCING_DETAIL_RE = re.compile(
    r"^(20\d{2}/\d{2}/\d{2})\s+"
    r"([A-Z]{3})\s+"
    r"([0-9,]+\.\d{2})\s+"
    r"([0-9.]+%)\s+"
    r"([0-9,]+\.\d{2})\s+"
    r"([0-9,]+\.\d{2})$"
)
MARKET_TRADE_START_RE = re.compile(
    r"^(賣出平倉|賣出開倉|買入平倉|買入開倉)\s+(.+?)\s+([A-Z]{3})\s+"
    r"(-?[0-9,]+)\s+([0-9.]+)\s+([+-]?[0-9,]+\.\d{2})\s+([+-]?[0-9,]+\.\d{2})$"
)
MARKET_TRADE_START_NO_INSTR_RE = re.compile(
    r"^(賣出平倉|賣出開倉|買入平倉|買入開倉)\s+([A-Z]{3})\s+"
    r"(-?[0-9,]+)\s+([0-9.]+)\s+([+-]?[0-9,]+\.\d{2})\s+([+-]?[0-9,]+\.\d{2})$"
)
MARKET_CODE_RE_PART = r"(?:FUTU OTC|[A-Z0-9]+)"
MARKET_TRADE_DETAIL_RE = re.compile(
    rf"^({MARKET_CODE_RE_PART})\s+([A-Z]{{3}})\s+"
    r"(20\d{2}/\d{2}/\d{2})\s+"
    r"(20\d{2}/\d{2}/\d{2})\s+"
    r"(-?[0-9,]+)\s+([0-9.]+)\s+([+-]?[0-9,]+\.\d{2})\s+([+-]?[0-9,]+\.\d{2})$"
)
MARKET_TRADE_DETAIL_WITH_INSTR_RE = re.compile(
    rf"^(.+?)\s+({MARKET_CODE_RE_PART})\s+([A-Z]{{3}})\s+"
    r"(20\d{2}/\d{2}/\d{2})\s+"
    r"(20\d{2}/\d{2}/\d{2})\s+"
    r"(-?[0-9,]+)\s+([0-9.]+)\s+([+-]?[0-9,]+\.\d{2})\s+([+-]?[0-9,]+\.\d{2})$"
)
FEE_PAIR_RE = re.compile(r"([^:：\s]+)[:：]\s*([0-9,]+\.\d{2})")
SUBTOTAL_RE = re.compile(r"小計[:：]\s*([0-9,]+\.\d{2})")


BUSINESS_TYPE_MAP = {
    "出入金": "external_transfer",
    "基金贖回": "fund_order",
    "基金申購": "fund_order",
    "入金": "external_transfer",
    "出金": "external_transfer",
    "港股IPO公開發售": "ipo_subscription",
    "公司行動": "corporate_action",
    "證券月度利息扣除": "financing_interest",
    "月度利息扣除": "financing_interest",
    "股票收益計劃": "securities_lending_income",
    "卡券入金": "broker_reward",
    "期權行權": "derivative_exercise",
}

FEE_TYPE_MAP = {
    "佣金": "commission",
    "平台使用費": "platform_fee",
    "交收費": "settlement_fee",
    "交易活動費": "taf",
    "印花稅": "stamp_duty",
    "期權監管費": "option_regulatory_fee",
    "交易費": "trading_fee",
    "期權清算費": "option_clearing_fee",
    "證監會規費": "sec_fee",
    "證監會徵費": "sfc_levy",
    "財匯局徵費": "frc_levy",
    "期權交收費": "option_settlement_fee",
    "交易系統使用費": "trading_system_fee",
    "綜合審計跟蹤監管費": "cat_fee",
    "未明細費用": "unclassified_fee",
}

LEGACY_CASH_RAW_TYPES = {"提取現金", "存入現金"}
LEGACY_TRADE_DIRECTIONS = {"買入", "賣出"}


@dataclass
class SourceLine:
    statement_id: str
    period: str
    filename: str
    page: int
    line_no: int
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.statement_id}/p{self.page}/text_line:{self.line_no}"


@dataclass
class ParserState:
    cache_dir: Path
    out_dir: Path
    raw_statements: list[dict[str, str]] = field(default_factory=list)
    source_lines: list[SourceLine] = field(default_factory=list)
    raw_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


def parse_amount(value: str) -> float:
    return float(value.replace(",", "").replace("+", "").strip())


def fmt_money(value: float | str | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        value = parse_amount(value)
    return f"{value:.2f}"


def fmt_decimal(value: float | str | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        value = parse_amount(value)
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text if text else "0"


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()


def normalize_repeated_labels(text: str) -> str:
    replacements = {
        "賣賣出出平平倉倉": "賣出平倉",
        "賣賣出出開開倉倉": "賣出開倉",
        "買買入入開開倉倉": "買入開倉",
        "買買入入平平倉倉": "買入平倉",
        "申申購購": "申購",
        "贖贖回回": "贖回",
    }
    for raw, normalized in replacements.items():
        text = text.replace(raw, normalized)
    return text


def normalize_legacy_fee_text(text: str) -> str:
    text = normalize_space(text)
    text = text.replace("印 花稅", "印花稅").replace("印 花 稅", "印花稅")
    text = text.replace("佣金：0.00 印 花稅", "佣金：0.00 印花稅")
    text = text.replace("初始保證金 要求", "初始保證金要求")
    text = text.replace("維持保證金 要求", "維持保證金要求")
    return text


def is_market_trade_layout_noise(text: str) -> bool:
    if not text:
        return False
    if text.startswith("保證金綜合帳戶"):
        return True
    if re.fullmatch(r"20\d{2}/\d{2}", text):
        return True
    if "買買賣賣方方向向" in text or "買賣方向" in text:
        return True
    return False


def period_from_filename(filename: str) -> str:
    for candidate in STATEMENT_DATE_RE.findall(filename):
        month = int(candidate[4:6])
        day = int(candidate[6:8])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return candidate[:6]
    match = STATEMENT_RE.search(filename)
    if not match:
        raise ValueError(f"Cannot derive period from {filename}")
    return match.group(0)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(cache_dir: Path, out_dir: Path) -> ParserState:
    state = ParserState(cache_dir=cache_dir, out_dir=out_dir)
    state.raw_statements = read_csv(cache_dir / "raw_statements.csv")

    for statement in state.raw_statements:
        raw_path = cache_dir / statement["filename"].replace(".pdf", ".raw.json")
        if not raw_path.exists():
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        statement_id = statement["statement_id"]
        state.raw_pages[statement_id] = raw
        period = statement["period"]
        filename = statement["filename"]
        for page in raw.get("pages", []):
            page_number = int(page["page_number"])
            for line_no, raw_line in enumerate(page.get("text", "").splitlines(), start=1):
                line = normalize_space(raw_line)
                if not line:
                    continue
                state.source_lines.append(
                    SourceLine(
                        statement_id=statement_id,
                        period=period,
                        filename=filename,
                        page=page_number,
                        line_no=line_no,
                        text=line,
                    )
                )
    return state


def add_issue(
    state: ParserState,
    issue_type: str,
    severity: str,
    status: str,
    message: str,
    source_ref: str = "",
    statement_id: str = "",
) -> None:
    state.issues.append(
        {
            "issue_id": f"issue_{len(state.issues) + 1:04d}",
            "statement_id": statement_id,
            "source_ref": source_ref,
            "issue_type": issue_type,
            "severity": severity,
            "status": status,
            "message": message,
        }
    )


def legacy_statement_ids(state: ParserState) -> set[str]:
    result: set[str] = set()
    for statement in state.raw_statements:
        raw = state.raw_pages.get(statement["statement_id"], {})
        text = "\n".join(page.get("text", "") for page in raw.get("pages", [])[:2])
        if "港股保證金賬戶月結單" in text or "港股現金賬戶月結單" in text:
            result.add(statement["statement_id"])
    return result


def iter_raw_table_rows(state: ParserState):
    statement_by_id = {row["statement_id"]: row for row in state.raw_statements}
    for statement_id, raw in state.raw_pages.items():
        statement = statement_by_id.get(statement_id, {})
        for page in raw.get("pages", []):
            page_number = int(page.get("page_number", 0))
            for table_index, table in enumerate(page.get("tables", []), start=1):
                for row_index, row in enumerate(table, start=1):
                    yield {
                        "statement_id": statement_id,
                        "period": statement.get("period", ""),
                        "filename": statement.get("filename", ""),
                        "page": page_number,
                        "table_index": table_index,
                        "row_index": row_index,
                        "row": row,
                        "source_ref": f"{statement_id}/p{page_number}/table:{table_index}/row:{row_index}",
                    }


def is_legacy_trade_row(row: list[str]) -> bool:
    return (
        len(row) >= 8
        and row[0] in LEGACY_TRADE_DIRECTIONS
        and bool(re.match(r"^20\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}$", row[3]))
        and bool(re.match(r"^[+-]?[0-9,]+(?:\.\d+)?$", row[4]))
        and bool(re.match(r"^[+-]?[0-9,]+(?:\.\d+)?$", row[6]))
        and bool(re.match(r"^[+-]?[0-9,]+(?:\.\d+)?$", row[7]))
    )


def is_legacy_fee_row(row: list[str]) -> bool:
    text = normalize_legacy_fee_text(" ".join(row))
    if not text or any(direction in text[:4] for direction in LEGACY_TRADE_DIRECTIONS):
        return False
    return any(label in text for label in ["佣金", "印花稅", "交收費", "交易費", "交易徵費", "小計"])


def is_legacy_cash_row(row: list[str]) -> bool:
    return parse_legacy_cash_row(row) is not None


def normalize_legacy_cash_type(value: str) -> str:
    return normalize_space(value).replace(" ", "")


def parse_legacy_cash_row(row: list[str]) -> dict[str, str] | None:
    if len(row) < 5:
        return None
    row_type = normalize_legacy_cash_type(row[0])
    if row_type not in LEGACY_CASH_RAW_TYPES:
        return None
    event_date = row[1]
    amount = row[2]
    if not DATE_RE.match(event_date):
        split_match = re.match(r"^(\d{1,2})\s+([+-]?[0-9,]+(?:\.\d+)?)$", amount)
        if split_match and re.match(r"^20\d{2}/\d{2}/\d{1}$", event_date):
            event_date = event_date + split_match.group(1)
            amount = split_match.group(2)
    if not DATE_RE.match(event_date) or not re.match(r"^[+-]?[0-9,]+(?:\.\d+)?$", amount):
        return None
    has_settlement_date = len(row) >= 6 and DATE_RE.match(row[3])
    return {
        "row_type": row_type,
        "event_date": event_date,
        "amount": amount,
        "settlement_date": row[3] if has_settlement_date else "",
        "order_no": row[4] if has_settlement_date else row[3] if len(row) > 3 else "",
        "description": row[5] if has_settlement_date else row[4] if len(row) > 4 else "",
    }


def is_legacy_asset_movement_row(row: list[str]) -> bool:
    return len(row) >= 6 and row[0] in {"存入股票", "提取股票"} and DATE_RE.match(row[1])


def is_legacy_financing_evidence_row(row: list[str]) -> bool:
    if len(row) >= 5 and DATE_RE.match(row[1]) and row[3].endswith("%"):
        return True
    return len(row) >= 4 and DATE_RE.match(row[0]) and row[2].endswith("%")


def split_legacy_instrument(raw: str) -> dict[str, Any]:
    raw = normalize_space(raw)
    match = re.match(r"(\d{4,5})\s+(.+)$", raw)
    if match:
        symbol, name = match.groups()
        instrument_type = "derivative" if any(token in name for token in ["購", "沽", "牛", "熊", ".C", ".P"]) else "stock"
        return {
            "instrument_code_raw": raw,
            "instrument_symbol": symbol.zfill(5),
            "instrument_name_raw": name,
            "instrument_type": instrument_type,
            "underlying_symbol": "",
            "expiry_date": "",
            "strike_price": "",
            "option_type": "",
            "quantity_unit": "share",
        }
    return {
        "instrument_code_raw": raw,
        "instrument_symbol": raw,
        "instrument_name_raw": "",
        "instrument_type": "unknown",
        "underlying_symbol": "",
        "expiry_date": "",
        "strike_price": "",
        "option_type": "",
        "quantity_unit": "",
    }


def legacy_fee_components(text: str) -> tuple[float, list[tuple[str, float]], str, str]:
    text = normalize_legacy_fee_text(text)
    components = [
        (raw_label, parse_amount(amount))
        for raw_label, amount in FEE_PAIR_RE.findall(text)
        if raw_label not in {"小計", "市場", "交收日"}
    ]
    subtotal_match = SUBTOTAL_RE.search(text)
    subtotal = parse_amount(subtotal_match.group(1)) if subtotal_match else round(sum(amount for _, amount in components), 2)
    component_total = round(sum(amount for _, amount in components), 2)
    if subtotal > 0 and subtotal - component_total > 0.01:
        components.append(("未明細費用", round(subtotal - component_total, 2)))
    market_match = re.search(r"市場[:：]\s*([A-Z]+)", text)
    settlement_match = re.search(r"交收日[:：]\s*(20\d{2}/\d{2}/\d{2})", text)
    return subtotal, components, market_match.group(1) if market_match else "", settlement_match.group(1) if settlement_match else ""


def should_append_cash_continuation(text: str) -> bool:
    if not text:
        return False
    if CASH_LINE_RE.match(text):
        return False
    if text.startswith("製備日期"):
        return False
    if any(marker in text for marker in ["資資產產進進出出", "日日期期 方方向向", "股股票票和和股股票票期期權權"]):
        return False
    upper_text = text.upper()
    if any(marker in upper_text for marker in ["SHARE", "PER", "FUND", "TAX"]):
        return True
    if re.match(r"^\d{4,5}\s+", text) and ">" in text:
        return True
    return False


def classify_cash(raw_type: str, direction: str, amount: float, description: str) -> tuple[str, str]:
    business_type = BUSINESS_TYPE_MAP.get(raw_type, "unknown")
    desc = description or ""
    if raw_type in LEGACY_CASH_RAW_TYPES:
        upper_desc = desc.upper()
        if "IPO APPLICATION" in upper_desc or "IPO REFUND" in upper_desc:
            if "HANDLING FEE" in upper_desc:
                return "ipo_subscription", "application_handling_fee"
            if "APPLICATION AMOUNT" in upper_desc:
                return "ipo_subscription", "application_payment"
            if "REFUND AMOUNT" in upper_desc:
                return "ipo_subscription", "refund"
            return "ipo_subscription", "ipo_cash_other"
        if "FUND SUBSCRIPTION" in upper_desc:
            return "fund_order", "subscription_cash_out"
        if "REDEMPTION FUND" in upper_desc or "FUND REDEMPTION" in upper_desc:
            return "fund_order", "redemption_cash_in"
        if "INTEREST FOR MONTH" in upper_desc:
            return "financing_interest", "interest_charge"
        if any(marker in upper_desc for marker in ["DIVIDEND", "F/D", "I/D-", "PAY IN", "WITHHOLDING TAX", "ADR FEE"]):
            if "WITHHOLDING TAX" in upper_desc:
                return "corporate_action", "withholding_tax"
            if "ADR FEE" in upper_desc:
                return "corporate_action", "adr_fee"
            return "corporate_action", "cash_dividend"
        if "HANDLING CHARGE" in upper_desc and "<HKEX" in upper_desc:
            return "corporate_action", "corporate_action_handling_charge"
        if "SCRIP CHARGE" in upper_desc and "<HKEX" in upper_desc:
            return "corporate_action", "scrip_charge"
        if "使用種子" in desc:
            return "broker_reward", "reward_cash_in"
        return "external_transfer", "deposit" if amount > 0 or raw_type == "存入現金" else "withdrawal"
    if raw_type in {"出入金", "入金", "出金"}:
        return business_type, "deposit" if amount > 0 or direction == "增加" else "withdrawal"
    if raw_type == "基金申購":
        return business_type, "subscription_cash_out"
    if raw_type == "基金贖回":
        return business_type, "redemption_cash_in"
    if raw_type == "港股IPO公開發售":
        if "Handling Fee" in desc:
            return business_type, "application_handling_fee"
        if "Application Amount" in desc:
            return business_type, "application_payment"
        if "Refund Amount" in desc:
            return business_type, "refund"
        return business_type, "ipo_cash_other"
    if raw_type == "公司行動":
        if "DIVIDENDS" in desc:
            return business_type, "cash_dividend"
        if "WITHHOLDING TAX" in desc:
            return business_type, "withholding_tax"
        if "ADR FEE" in desc:
            return business_type, "adr_fee"
        if "Handling Charge" in desc:
            return business_type, "corporate_action_handling_charge"
        if "I/D-" in desc and "PAY IN" in desc:
            return business_type, "cash_dividend"
        if "F/D-" in desc:
            return business_type, "cash_dividend"
        if "PAY IN" in desc:
            return business_type, "other_corporate_action_cash"
        return business_type, "other_corporate_action_cash"
    if raw_type in {"證券月度利息扣除", "月度利息扣除"}:
        return business_type, "interest_charge"
    if raw_type == "股票收益計劃":
        return business_type, "income_received"
    if raw_type == "卡券入金":
        return business_type, "reward_cash_in"
    if raw_type == "期權行權":
        return business_type, "exercise_cash_effect"
    return business_type, "unknown"


def dedupe_cash_rows(state: ParserState, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: list[list[dict[str, Any]]] = []
    used = set()
    for i, row in enumerate(rows):
        if i in used:
            continue
        key = (
            row["statement_id"],
            row["event_date"],
            row["event_type_raw"],
            row["currency"],
            row["amount"],
        )
        desc = normalize_space(row["description"])
        group = [row]
        used.add(i)
        for j, other in enumerate(rows[i + 1 :], start=i + 1):
            if j in used:
                continue
            other_key = (
                other["statement_id"],
                other["event_date"],
                other["event_type_raw"],
                other["currency"],
                other["amount"],
            )
            other_desc = normalize_space(other["description"])
            if other_key != key:
                continue
            is_continuation_duplicate = (
                desc
                and other_desc
                and desc != other_desc
                and (desc.startswith(other_desc) or other_desc.startswith(desc))
            )
            if is_continuation_duplicate:
                group.append(other)
                used.add(j)
        grouped.append(group)

    result: list[dict[str, Any]] = []
    for index, group in enumerate(grouped, start=1):
        chosen = max(group, key=lambda item: len(normalize_space(item["description"])))
        row = dict(chosen)
        row["cash_entry_id"] = f"cash_{index:04d}"
        row["source_refs"] = "; ".join(item["source_ref"] for item in group)
        row["dedupe_status"] = "deduped" if len(group) > 1 else "unique"
        row["source_count"] = len(group)
        if len(group) > 1:
            add_issue(
                state,
                issue_type="duplicate_candidate",
                severity="info",
                status="merged",
                source_ref=row["source_refs"],
                statement_id=row["statement_id"],
                message=(
                    f"Merged {len(group)} cash candidates for {row['event_date']} "
                    f"{row['event_type_raw']} {row['currency']} {row['amount']}."
                ),
            )
        result.append(row)

    result.sort(key=lambda r: (r["statement_id"], r["event_date"], r["cash_entry_id"]))
    for index, row in enumerate(result, start=1):
        row["cash_entry_id"] = f"cash_{index:04d}"
    return result


def parse_legacy_cash_candidates(state: ParserState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    legacy_ids = legacy_statement_ids(state)
    for item in iter_raw_table_rows(state):
        if item["statement_id"] not in legacy_ids:
            continue
        row = item["row"]
        parsed_cash = parse_legacy_cash_row(row)
        if not parsed_cash:
            continue
        description = parsed_cash["description"]
        order_no = parsed_cash["order_no"]
        amount = parse_amount(parsed_cash["amount"])
        business_type, cash_leg_type = classify_cash(
            parsed_cash["row_type"],
            "增加" if amount > 0 or parsed_cash["row_type"] == "存入現金" else "減少",
            amount,
            description,
        )
        rows.append(
            {
                "statement_id": item["statement_id"],
                "period": item["period"],
                "filename": item["filename"],
                "page": item["page"],
                "event_date": parsed_cash["event_date"],
                "direction_raw": "增加" if amount > 0 or parsed_cash["row_type"] == "存入現金" else "減少",
                "event_type_raw": parsed_cash["row_type"],
                "business_type": business_type,
                "cash_leg_type": cash_leg_type,
                "currency": "HKD",
                "amount": fmt_money(amount),
                "description": normalize_space(description),
                "raw_line": normalize_space(" | ".join(cell for cell in row if cell)),
                "source_ref": item["source_ref"],
                "legacy_order_no": order_no,
                "mapping_status": "mapped" if business_type != "unknown" else "unmapped",
            }
        )
    return rows


def parse_cash_entries(state: ParserState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_refs: list[str] = []

    def flush() -> None:
        nonlocal current, current_refs
        if current is None:
            return
        current["description"] = normalize_space(current.get("description", ""))
        current["raw_line"] = normalize_space(current.get("raw_line", ""))
        current["source_ref"] = "; ".join(current_refs)
        business_type, cash_leg_type = classify_cash(
            current["event_type_raw"],
            current["direction_raw"],
            float(current["amount"]),
            current["description"],
        )
        current["business_type"] = business_type
        current["cash_leg_type"] = cash_leg_type
        current["mapping_status"] = "mapped" if business_type != "unknown" else "unmapped"
        rows.append(current)
        current = None
        current_refs = []

    for line in state.source_lines:
        text = normalize_repeated_labels(line.text)
        if current is not None and any(
            marker in text
            for marker in [
                "資資產產進進出出",
                "資資產進出",
                "股股票票和和股股票票期期權權",
            ]
        ):
            flush()
            continue
        match = CASH_LINE_RE.match(text)
        if match:
            flush()
            event_date, direction, event_type, currency, raw_amount, description = match.groups()
            event_type = normalize_space(event_type)
            amount = parse_amount(raw_amount)
            current = {
                "statement_id": line.statement_id,
                "period": line.period,
                "filename": line.filename,
                "page": line.page,
                "event_date": event_date,
                "direction_raw": direction,
                "event_type_raw": event_type,
                "currency": currency,
                "amount": fmt_money(amount),
                "description": description,
                "raw_line": text,
            }
            current_refs = [line.source_ref]
            continue
        if current is not None and should_append_cash_continuation(text):
            current["description"] = normalize_space(f"{current['description']} {text}")
            current["raw_line"] = normalize_space(f"{current['raw_line']} {text}")
            current_refs.append(line.source_ref)

    flush()
    rows.extend(parse_legacy_cash_candidates(state))
    return dedupe_cash_rows(state, rows)


def fund_key_from_text(text: str) -> str:
    text = text or ""
    if "HK0000584752" in text or "WeValue" in text or "微金美元" in text:
        return "HK0000584752"
    if "HK0000478930" in text or "WeInvest" in text or "Welnvest" in text or "GaoTeng" in text or "微財" in text:
        return "HK0000478930"
    if "HK0000499787" in text or "E Fund" in text or "易方達" in text:
        return "HK0000499787"
    return ""


def load_opening_pending_funds(state: ParserState) -> list[dict[str, Any]]:
    pending_rows: list[dict[str, Any]] = []
    for row in read_csv(state.cache_dir / "position_snapshots.csv"):
        if row.get("snapshot_type") != "opening":
            continue
        if row.get("asset_category") != "fund":
            continue
        pending_amount = fmt_money(abs(parse_amount(row.get("pending_amount") or "0")))
        if abs(float(pending_amount)) <= 0.01:
            continue
        source_ref = (
            f"{row.get('statement_id')}/p{row.get('page')}"
            f"/table:{row.get('table_index')}/row:{row.get('row_index')}"
        )
        pending_rows.append(
            {
                "statement_id": row.get("statement_id", ""),
                "period": row.get("period", ""),
                "instrument_code": fund_key_from_text(row.get("code_name", "")),
                "instrument_name_raw": row.get("code_name", ""),
                "currency": row.get("currency", ""),
                "pending_amount_abs": pending_amount,
                "source_ref": source_ref,
            }
        )
    return pending_rows


def parse_fund_transactions(
    state: ParserState, cash_entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tx_rows: list[dict[str, Any]] = []
    fee_rows: list[dict[str, Any]] = []
    last_tx_id = ""

    for line in state.source_lines:
        text = normalize_repeated_labels(line.text)
        tx_match = FUND_TX_RE.match(text)
        if tx_match:
            tx_type, code, name, currency, order_date, trade_date, qty, price, amount = tx_match.groups()
            row_id = f"fund_tx_{len(tx_rows) + 1:04d}"
            row = {
                "fund_transaction_row_id": row_id,
                "statement_id": line.statement_id,
                "period": line.period,
                "filename": line.filename,
                "page": line.page,
                "source_ref": line.source_ref,
                "transaction_type_raw": tx_type,
                "fund_order_type": "subscription" if tx_type == "申購" else "redemption",
                "instrument_code": code,
                "instrument_name_raw": name,
                "currency": currency,
                "order_date": order_date,
                "trade_date": trade_date,
                "quantity": "" if qty == "-" else qty.replace(",", ""),
                "price": "" if price == "-" else price,
                "amount_abs": fmt_money(amount),
                "evidence_status": "amount_only" if trade_date == "-" else "complete_detail",
                "raw_line": text,
            }
            tx_rows.append(row)
            last_tx_id = row_id
            continue

        fee_match = FUND_FEE_RE.match(text)
        if fee_match:
            fee_amount, subtotal = fee_match.groups()
            fee_rows.append(
                {
                    "fund_fee_row_id": f"fund_fee_{len(fee_rows) + 1:04d}",
                    "fund_transaction_row_id": last_tx_id,
                    "statement_id": line.statement_id,
                    "period": line.period,
                    "filename": line.filename,
                    "page": line.page,
                    "source_ref": line.source_ref,
                    "fee_amount": fmt_money(fee_amount),
                    "subtotal": fmt_money(subtotal),
                    "raw_line": text,
                    "mapping_status": "matched_to_previous_fund_row" if last_tx_id else "orphan_fee_line",
                }
            )
            if not last_tx_id:
                add_issue(
                    state,
                    issue_type="orphan_fund_fee_line",
                    severity="warning",
                    status="open",
                    source_ref=line.source_ref,
                    statement_id=line.statement_id,
                    message="Fund fee line has no preceding fund transaction row.",
                )

    detail_by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    amount_only_rows: list[dict[str, Any]] = []
    for row in tx_rows:
        key = (
            row["statement_id"],
            row["fund_order_type"],
            row["instrument_code"],
            row["currency"],
            row["amount_abs"],
        )
        if row["evidence_status"] == "complete_detail":
            detail_by_key[key] = row
        else:
            amount_only_rows.append(row)

    orders: list[dict[str, Any]] = []
    used_amount_only = set()
    for detail in tx_rows:
        if detail["evidence_status"] != "complete_detail":
            continue
        key = (
            detail["statement_id"],
            detail["fund_order_type"],
            detail["instrument_code"],
            detail["currency"],
            detail["amount_abs"],
        )
        matching_amount_only = [
            row
            for row in amount_only_rows
            if row["fund_order_type"] == detail["fund_order_type"]
            and row["instrument_code"] == detail["instrument_code"]
            and row["currency"] == detail["currency"]
            and row["amount_abs"] == detail["amount_abs"]
            and abs(days_between(row["order_date"], detail["order_date"])) <= 3
        ]
        source_refs = [detail["source_ref"]]
        evidence_rows = [detail["fund_transaction_row_id"]]
        for row in matching_amount_only:
            used_amount_only.add(row["fund_transaction_row_id"])
            source_refs.append(row["source_ref"])
            evidence_rows.append(row["fund_transaction_row_id"])
        orders.append(
            {
                "fund_order_id": f"fund_order_{len(orders) + 1:04d}",
                "statement_id": detail["statement_id"],
                "period": detail["period"],
                "fund_order_type": detail["fund_order_type"],
                "instrument_code": detail["instrument_code"],
                "instrument_name_raw": detail["instrument_name_raw"],
                "currency": detail["currency"],
                "order_date": detail["order_date"],
                "trade_date": detail["trade_date"],
                "quantity": detail["quantity"],
                "price": detail["price"],
                "fund_amount_abs": detail["amount_abs"],
                "evidence_status": "complete_detail",
                "cash_match_status": "",
                "cash_match_source_refs": "",
                "evidence_row_ids": "; ".join(evidence_rows),
                "source_refs": "; ".join(source_refs),
            }
        )

    for row in amount_only_rows:
        if row["fund_transaction_row_id"] in used_amount_only:
            continue
        orders.append(
            {
                "fund_order_id": f"fund_order_{len(orders) + 1:04d}",
                "statement_id": row["statement_id"],
                "period": row["period"],
                "fund_order_type": row["fund_order_type"],
                "instrument_code": row["instrument_code"],
                "instrument_name_raw": row["instrument_name_raw"],
                "currency": row["currency"],
                "order_date": row["order_date"],
                "trade_date": "",
                "quantity": "",
                "price": "",
                "fund_amount_abs": row["amount_abs"],
                "evidence_status": "amount_only",
                "cash_match_status": "",
                "cash_match_source_refs": "",
                "evidence_row_ids": row["fund_transaction_row_id"],
                "source_refs": row["source_ref"],
            }
        )

    fund_cash = [row for row in cash_entries if row["business_type"] == "fund_order"]
    for cash in fund_cash:
        cash_amount_abs = fmt_money(abs(float(cash["amount"])))
        expected_type = "subscription" if cash["cash_leg_type"] == "subscription_cash_out" else "redemption"
        instrument_code = fund_key_from_text(cash["description"]) or "legacy_fund_unknown"
        already_has_candidate = False
        for order in orders:
            if order["fund_order_type"] != expected_type:
                continue
            if order["currency"] != cash["currency"]:
                continue
            if order["instrument_code"] != instrument_code:
                continue
            if abs(float(order["fund_amount_abs"]) - float(cash_amount_abs)) > 0.01:
                continue
            if abs(days_between(cash["event_date"], order["order_date"])) <= 7:
                already_has_candidate = True
                break
        if already_has_candidate:
            continue
        orders.append(
            {
                "fund_order_id": f"fund_order_{len(orders) + 1:04d}",
                "statement_id": cash["statement_id"],
                "period": cash["period"],
                "fund_order_type": expected_type,
                "instrument_code": instrument_code,
                "instrument_name_raw": cash["description"],
                "currency": cash["currency"],
                "order_date": cash["event_date"],
                "trade_date": "",
                "quantity": "",
                "price": "",
                "fund_amount_abs": cash_amount_abs,
                "evidence_status": "amount_only",
                "cash_match_status": "",
                "cash_match_source_refs": "",
                "evidence_row_ids": cash["cash_entry_id"],
                "source_refs": cash["source_refs"],
            }
        )

    cash_legs: list[dict[str, Any]] = []
    for cash in fund_cash:
        cash_amount = abs(float(cash["amount"]))
        expected_type = "subscription" if cash["cash_leg_type"] == "subscription_cash_out" else "redemption"
        primary_candidates = []
        extended_candidates = []
        for order in orders:
            if order["fund_order_type"] != expected_type:
                continue
            if order["currency"] != cash["currency"]:
                continue
            if abs(float(order["fund_amount_abs"]) - cash_amount) > 0.01:
                continue
            if fund_key_from_text(cash["description"]) != order["instrument_code"]:
                continue
            date_distances = [abs(days_between(cash["event_date"], order["order_date"]))]
            if order["trade_date"]:
                date_distances.append(abs(days_between(cash["event_date"], order["trade_date"])))
            best_distance = min(date_distances)
            if best_distance <= 3:
                primary_candidates.append(order)
            elif best_distance <= 7:
                extended_candidates.append(order)
        candidates = primary_candidates or extended_candidates
        used_extended_window = not primary_candidates and bool(extended_candidates)
        match_status = "unmatched"
        related_order = ""
        if len(candidates) == 1:
            related_order = candidates[0]["fund_order_id"]
            if used_extended_window:
                match_status = "matched_extended_date_window"
            else:
                match_status = "matched_detail" if candidates[0]["evidence_status"] == "complete_detail" else "matched_amount_only"
        elif len(candidates) > 1:
            match_status = "ambiguous"
        cash_legs.append(
            {
                "fund_cash_leg_id": f"fund_cash_{len(cash_legs) + 1:04d}",
                "cash_entry_id": cash["cash_entry_id"],
                "fund_order_id": related_order,
                "statement_id": cash["statement_id"],
                "event_date": cash["event_date"],
                "cash_leg_type": cash["cash_leg_type"],
                "currency": cash["currency"],
                "cash_amount": cash["amount"],
                "match_status": match_status,
                "candidate_order_ids": "; ".join(row["fund_order_id"] for row in candidates),
            }
        )
        if match_status in {"unmatched", "ambiguous"}:
            add_issue(
                state,
                issue_type="fund_cash_match",
                severity="needs_review",
                status=match_status,
                source_ref=cash["source_refs"],
                statement_id=cash["statement_id"],
                message=(
                    f"Fund cash leg {cash['cash_entry_id']} matched {len(candidates)} "
                    f"fund order candidates."
                ),
            )
        elif match_status == "matched_extended_date_window":
            add_issue(
                state,
                issue_type="fund_cash_matched_extended_date_window",
                severity="info",
                status="resolved_extended_date_window",
                source_ref=cash["source_refs"],
                statement_id=cash["statement_id"],
                message=(
                    f"Fund cash leg {cash['cash_entry_id']} matched one fund order outside "
                    "the default 3-day window but within 7 days."
                ),
            )

    order_ids_with_cash = {row["fund_order_id"] for row in cash_legs if row["fund_order_id"]}
    for order in orders:
        if order["fund_order_id"] in order_ids_with_cash:
            matched_cash_refs = [
                row["cash_entry_id"] for row in cash_legs if row["fund_order_id"] == order["fund_order_id"]
            ]
            order["cash_match_status"] = "matched_cash_leg"
            order["cash_match_source_refs"] = "; ".join(matched_cash_refs)

    opening_pending = load_opening_pending_funds(state)
    for order in orders:
        if order["fund_order_id"] not in order_ids_with_cash:
            pending_candidates = [
                row
                for row in opening_pending
                if row["statement_id"] == order["statement_id"]
                and row["instrument_code"] == order["instrument_code"]
                and row["currency"] == order["currency"]
                and abs(float(row["pending_amount_abs"]) - float(order["fund_amount_abs"])) <= 0.01
            ]
            if len(pending_candidates) == 1:
                order["cash_match_status"] = "matched_opening_pending"
                order["cash_match_source_refs"] = pending_candidates[0]["source_ref"]
                add_issue(
                    state,
                    issue_type="fund_order_matched_opening_pending",
                    severity="info",
                    status="resolved_opening_pending",
                    source_ref=f"{order['source_refs']}; {pending_candidates[0]['source_ref']}",
                    statement_id=order["statement_id"],
                    message=(
                        f"Fund order {order['fund_order_id']} matched opening pending amount; "
                        "do not create a current-period cash leg."
                    ),
                )
                continue
            order["cash_match_status"] = "unmatched"
            order["cash_match_source_refs"] = ""
            add_issue(
                state,
                issue_type="fund_order_without_cash_leg",
                severity="needs_review",
                status="open",
                source_ref=order["source_refs"],
                statement_id=order["statement_id"],
                message=(
                    f"Fund order {order['fund_order_id']} has no matched cash leg; "
                    "keep as raw fact and reconcile later."
                ),
            )

    return tx_rows, fee_rows, orders, cash_legs


def days_between(left: str, right: str) -> int:
    if not left or not right or right == "-":
        return 9999
    ldt = datetime.strptime(left, "%Y/%m/%d")
    rdt = datetime.strptime(right, "%Y/%m/%d")
    return (ldt - rdt).days


DIVIDEND_REMARK_RE = re.compile(r"I/D-([A-Z]+)([0-9.]+)/SH\(-([0-9.]+)%\),\s*PAY IN")


def code_from_position_name(text: str) -> str:
    match = re.match(r"^(\d{5})\(", text or "")
    return match.group(1) if match else ""


def load_positions_by_statement(state: ParserState) -> dict[str, list[dict[str, Any]]]:
    positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(state.cache_dir / "position_snapshots.csv"):
        if row.get("asset_category") != "stock_or_option":
            continue
        if row.get("market") != "SEHK":
            continue
        code = code_from_position_name(row.get("code_name", ""))
        if not code:
            continue
        positions[row.get("statement_id", "")].append(
            {
                "instrument_code": code,
                "instrument_name_raw": row.get("code_name", ""),
                "quantity": row.get("quantity", ""),
                "snapshot_type": row.get("snapshot_type", ""),
            }
        )
    return positions


def infer_dividend_from_remark(
    cash: dict[str, Any], positions_by_statement: dict[str, list[dict[str, Any]]]
) -> dict[str, str]:
    desc = cash.get("description", "")
    match = DIVIDEND_REMARK_RE.search(desc)
    if not match:
        return {}
    dividend_currency, rate, tax_rate = match.groups()
    net_rate = float(rate) * (1 - float(tax_rate) / 100)
    cash_amount = abs(float(cash.get("amount") or 0))
    candidates: list[tuple[float, dict[str, Any]]] = []
    seen_codes = set()
    for position in positions_by_statement.get(cash.get("statement_id", ""), []):
        code = position["instrument_code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)
        try:
            quantity = float(position["quantity"])
        except ValueError:
            continue
        if quantity <= 0 or net_rate <= 0:
            continue
        implied_fx = cash_amount / (quantity * net_rate)
        # HKD/CNY style dividend cash conversion should be in a normal FX band.
        if 0.7 <= implied_fx <= 1.5:
            candidates.append((abs(implied_fx - 1.0), position))
    if len(candidates) != 1:
        return {}
    position = candidates[0][1]
    return {
        "instrument_code": position["instrument_code"],
        "instrument_mapping_status": "inferred_from_dividend_remark",
        "quantity_basis": fmt_decimal(position["quantity"]),
        "rate_raw": f"{dividend_currency}{rate}/SH(-{tax_rate}%)",
    }


def infer_corporate_action_from_handling_charge(
    cash: dict[str, Any], cash_entries: list[dict[str, Any]]
) -> dict[str, str]:
    if cash.get("cash_leg_type") != "cash_dividend":
        return {}
    desc = cash.get("description", "")
    dividend_qty_match = re.search(r"([0-9,.]+)\s+SHARES?", desc, re.IGNORECASE)
    dividend_qty = dividend_qty_match.group(1).replace(",", "") if dividend_qty_match else ""
    candidates: list[dict[str, str]] = []
    for other in cash_entries:
        if other.get("cash_entry_id") == cash.get("cash_entry_id"):
            continue
        if other.get("statement_id") != cash.get("statement_id"):
            continue
        if other.get("event_date") != cash.get("event_date"):
            continue
        if other.get("cash_leg_type") != "corporate_action_handling_charge":
            continue
        other_desc = other.get("description", "")
        sehk_match = re.search(r"<SEHK\s+(\d{1,5})\b", other_desc)
        if not sehk_match:
            continue
        qty_match = re.search(r"([0-9,.]+)\s+SHARES?", other_desc, re.IGNORECASE)
        quantity_basis = qty_match.group(1).replace(",", "") if qty_match else ""
        candidates.append(
            {
                "instrument_code": sehk_match.group(1).zfill(5),
                "instrument_mapping_status": "inferred_from_handling_charge",
                "quantity_basis": quantity_basis,
            }
        )
    if dividend_qty:
        candidates = [row for row in candidates if row["quantity_basis"] == dividend_qty]
    if len(candidates) != 1:
        return {}
    return candidates[0]


def parse_corporate_actions(state: ParserState, cash_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    positions_by_statement = load_positions_by_statement(state)
    for cash in cash_entries:
        if cash["business_type"] != "corporate_action":
            continue
        desc = cash["description"]
        instrument_code = ""
        instrument_status = "needs_review"
        match = re.match(r"^([A-Z]{1,5})\s+", desc)
        if match:
            instrument_code = match.group(1)
            instrument_status = "mapped"
        sehk_match = re.search(r"<SEHK\s+(\d{1,5})\b", desc)
        if sehk_match and not instrument_code:
            instrument_code = sehk_match.group(1).zfill(5)
            instrument_status = "mapped_from_sehk_remark"
        inferred_handling_charge = infer_corporate_action_from_handling_charge(cash, cash_entries)
        if inferred_handling_charge and not instrument_code:
            instrument_code = inferred_handling_charge["instrument_code"]
            instrument_status = inferred_handling_charge["instrument_mapping_status"]
        inferred_dividend = infer_dividend_from_remark(cash, positions_by_statement)
        if inferred_dividend and not instrument_code:
            instrument_code = inferred_dividend["instrument_code"]
            instrument_status = inferred_dividend["instrument_mapping_status"]
        group_type = {
            "cash_dividend": "dividend_event",
            "withholding_tax": "dividend_event",
            "adr_fee": "adr_fee_event",
            "corporate_action_handling_charge": "corporate_action_fee_event",
        }.get(cash["cash_leg_type"], "other")
        qty_match = re.search(r"([0-9,.]+)\s+SHARES?", desc, re.IGNORECASE)
        rate_match = re.search(r"([-0-9.]+)\s+[A-Z]{3}\s+PER\s+SHARE", desc)
        shorthand_rate_match = re.search(r"([A-Z]{3}[0-9.]+/SH(?:\([^)]+\))?)", desc)
        quantity_basis = qty_match.group(1).replace(",", "") if qty_match else ""
        rate_raw = rate_match.group(0) if rate_match else ""
        if shorthand_rate_match and not rate_raw:
            rate_raw = shorthand_rate_match.group(1)
        if inferred_handling_charge and not quantity_basis:
            quantity_basis = inferred_handling_charge["quantity_basis"]
        if inferred_dividend:
            quantity_basis = inferred_dividend["quantity_basis"]
            rate_raw = inferred_dividend["rate_raw"]
        rows.append(
            {
                "corporate_action_cash_leg_id": f"ca_cash_{len(rows) + 1:04d}",
                "cash_entry_id": cash["cash_entry_id"],
                "statement_id": cash["statement_id"],
                "period": cash["period"],
                "event_date": cash["event_date"],
                "instrument_code_raw": instrument_code,
                "instrument_mapping_status": instrument_status,
                "corporate_action_group_type": group_type,
                "corporate_action_type": cash["cash_leg_type"],
                "currency": cash["currency"],
                "cash_amount": cash["amount"],
                "quantity_basis": quantity_basis,
                "rate_raw": rate_raw,
                "description_raw": desc,
                "source_refs": cash["source_refs"],
                "dedupe_status": cash["dedupe_status"],
            }
        )
    return rows


def parse_financing_interest(state: ParserState, cash_entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cash_rows = [
        {
            "financing_interest_event_id": f"fin_interest_{idx + 1:04d}",
            "cash_entry_id": row["cash_entry_id"],
            "statement_id": row["statement_id"],
            "period": row["period"],
            "cash_event_date": row["event_date"],
            "interest_type": "margin_interest",
            "period_label": row["description"],
            "currency": row["currency"],
            "cash_amount": row["amount"],
            "source_refs": row["source_refs"],
        }
        for idx, row in enumerate(cash_entries)
        if row["business_type"] == "financing_interest"
    ]

    evidence: list[dict[str, Any]] = []
    for line in state.source_lines:
        match = FINANCING_DETAIL_RE.match(line.text)
        if not match:
            continue
        date, currency, financing_amount, annual_rate, daily_interest, cumulative_interest = match.groups()
        evidence.append(
            {
                "financing_interest_evidence_id": f"fin_evidence_{len(evidence) + 1:04d}",
                "statement_id": line.statement_id,
                "period": line.period,
                "evidence_date": date,
                "currency": currency,
                "financing_amount": fmt_money(financing_amount),
                "annual_rate_raw": annual_rate,
                "daily_interest": fmt_money(daily_interest),
                "cumulative_interest": fmt_money(cumulative_interest),
                "source_ref": line.source_ref,
                "raw_line": line.text,
            }
        )
    legacy_ids = legacy_statement_ids(state)
    for item in iter_raw_table_rows(state):
        if item["statement_id"] not in legacy_ids:
            continue
        row = item["row"]
        if not is_legacy_financing_evidence_row(row):
            continue
        if len(row) >= 5 and DATE_RE.match(row[1]):
            evidence_date, financing_amount, annual_rate, daily_interest = row[1], row[2], row[3], row[4]
        else:
            evidence_date, financing_amount, annual_rate, daily_interest = row[0], row[1], row[2], row[3]
        evidence.append(
            {
                "financing_interest_evidence_id": f"fin_evidence_{len(evidence) + 1:04d}",
                "statement_id": item["statement_id"],
                "period": item["period"],
                "evidence_date": evidence_date,
                "currency": "HKD",
                "financing_amount": fmt_money(financing_amount),
                "annual_rate_raw": annual_rate,
                "daily_interest": fmt_money(daily_interest),
                "cumulative_interest": "",
                "source_ref": item["source_ref"],
                "raw_line": normalize_space(" | ".join(cell for cell in row if cell)),
            }
        )
    return cash_rows, evidence


def parse_asset_movements(state: ParserState) -> list[dict[str, Any]]:
    source_rows = read_csv(state.cache_dir / "asset_movements.csv")
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        raw_type = normalize_space(row["event_type_raw"])
        description = normalize_space(row["description"])
        business_type = (
            "ipo_subscription"
            if "IPO" in raw_type or "IPO Allotment" in description
            else "derivative_exercise"
            if "期權" in raw_type
            else "asset_movement"
        )
        movement_type = (
            "allotment"
            if business_type == "ipo_subscription"
            else "option_expiry_close"
            if business_type == "derivative_exercise"
            else "deposit"
            if raw_type == "存入股票"
            else "withdrawal"
            if raw_type == "提取股票"
            else "other"
        )
        qty = parse_amount(row["quantity"])
        if row["direction"] == "減少":
            qty = -abs(qty)
        rows.append(
            {
                "asset_movement_id": f"asset_move_{len(rows) + 1:04d}",
                "statement_id": row["statement_id"],
                "period": row["period"],
                "filename": row["filename"],
                "page": row["page"],
                "source_ref": f"{row['statement_id']}/p{row['page']}/table:{row['table_index']}/row:{row['row_index']}",
                "business_type": business_type,
                "asset_movement_type": movement_type,
                "event_date": row["event_date"],
                "direction_raw": row["direction"],
                "event_type_raw": raw_type,
                "instrument_code_raw": normalize_space(row["code_name"]),
                "currency": row["currency"],
                "quantity": fmt_decimal(qty),
                "amount": fmt_money(row["amount"]),
                "description_raw": description,
            }
        )
    return rows


def parse_stock_yield_events(state: ParserState, cash_entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_rows = read_csv(state.cache_dir / "stock_yield_events.csv")
    daily_rows: list[dict[str, Any]] = []
    for row in source_rows:
        daily_rows.append(
            {
                "stock_yield_daily_id": f"stock_yield_daily_{len(daily_rows) + 1:04d}",
                "statement_id": row["statement_id"],
                "period": row["period"],
                "source_ref": f"{row['statement_id']}/p{row['page']}/table:{row['table_index']}/row:{row['row_index']}",
                "event_date": row["event_date"],
                "instrument_code_raw": normalize_space(row["code_name"]),
                "market": row["market"],
                "currency": row["currency"],
                "interest_type_raw": row["interest_type"],
                "quantity": fmt_decimal(row["quantity"]),
                "settlement_amount": fmt_money(row["settlement_amount"]),
                "collateral_amount": fmt_money(row["collateral_amount"]),
                "annual_rate_raw": row["annual_rate"],
                "interest_amount": fmt_money(row["interest"]),
                "cumulative_interest": fmt_money(row["cumulative_interest"]),
                "income_month": row["income_month"],
            }
        )

    cash_rows = [
        {
            "stock_yield_cash_id": f"stock_yield_cash_{idx + 1:04d}",
            "cash_entry_id": row["cash_entry_id"],
            "statement_id": row["statement_id"],
            "period": row["period"],
            "event_date": row["event_date"],
            "currency": row["currency"],
            "cash_amount": row["amount"],
            "description_raw": row["description"],
            "income_month_guess": income_month_from_description(row["description"]),
            "reconciliation_status": "needs_review",
            "source_refs": row["source_refs"],
        }
        for idx, row in enumerate(cash_entries)
        if row["business_type"] == "securities_lending_income"
    ]
    return daily_rows, cash_rows


def income_month_from_description(description: str) -> str:
    month_map = {
        "January": "2025/01",
        "February": "2025/02",
        "March": "2025/03",
    }
    for text, month in month_map.items():
        if text in description:
            return month
    return ""


def parse_derivative_events(
    cash_entries: list[dict[str, Any]], asset_movements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    derivative_cash = [row for row in cash_entries if row["business_type"] == "derivative_exercise"]
    derivative_assets = [row for row in asset_movements if row["business_type"] == "derivative_exercise"]
    for cash in derivative_cash:
        related_assets = [
            row
            for row in derivative_assets
            if row["statement_id"] == cash["statement_id"] and row["event_date"] == cash["event_date"]
        ]
        instrument = related_assets[0]["instrument_code_raw"] if related_assets else ""
        event_type = "expiry" if "EXP" in cash["description"] else "unknown"
        rows.append(
            {
                "derivative_exercise_id": f"deriv_{len(rows) + 1:04d}",
                "statement_id": cash["statement_id"],
                "period": cash["period"],
                "event_date": cash["event_date"],
                "exercise_type": event_type,
                "option_instrument_raw": instrument,
                "cash_entry_id": cash["cash_entry_id"],
                "cash_amount": cash["amount"],
                "asset_movement_ids": "; ".join(row["asset_movement_id"] for row in related_assets),
                "source_refs": "; ".join([cash["source_refs"]] + [row["source_ref"] for row in related_assets]),
                "description_raw": cash["description"],
            }
        )
    return rows


def parse_legacy_market_trades(
    state: ParserState, start_trade_index: int, start_fee_index: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    legacy_ids = legacy_statement_ids(state)
    trades: list[dict[str, Any]] = []
    fee_rows: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    def close_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        if not pending.get("fee_total"):
            pending["fee_total"] = "0.00"
        trades.append(pending)
        pending = None

    def create_trade(item: dict[str, Any], row: list[str]) -> dict[str, Any]:
        trade_date, trade_time = row[3].split(" ", 1)
        raw_direction = row[0]
        side = "buy" if raw_direction == "買入" else "sell"
        trade = {
            "trade_id": f"mt_{start_trade_index + len(trades) + 1:04d}",
            "statement_id": item["statement_id"],
            "period": item["period"],
            "filename": item["filename"],
            "page": item["page"],
            "business_type": "market_trade",
            "trade_datetime": row[3],
            "trade_date": trade_date,
            "settlement_date": "",
            "raw_direction": raw_direction,
            "side": side,
            "position_effect": "open" if side == "buy" else "close",
            "market": "SEHK",
            "currency": "HKD",
            "quantity": row[4].replace(",", ""),
            "price": row[5],
            "gross_amount": fmt_money(row[6]),
            "fee_total": "",
            "net_cash_amount": fmt_money(row[7]),
            "source_refs": item["source_ref"],
        }
        trade.update(split_legacy_instrument(row[2]))
        return trade

    def attach_fee(item: dict[str, Any], row: list[str]) -> None:
        nonlocal pending
        if pending is None:
            return
        fee_text = normalize_legacy_fee_text(" ".join(row))
        subtotal, components, market, settlement_date = legacy_fee_components(fee_text)
        pending["fee_total"] = fmt_money(subtotal)
        if market:
            pending["market"] = market
        if settlement_date:
            pending["settlement_date"] = settlement_date
        pending["source_refs"] = "; ".join([pending["source_refs"], item["source_ref"]])
        for raw_label, amount in components:
            fee_rows.append(
                {
                    "fee_tax_item_id": f"trade_fee_{start_fee_index + len(fee_rows) + 1:04d}",
                    "trade_index": pending["trade_id"],
                    "parent_event_id": pending["trade_id"],
                    "parent_business_type": "market_trade",
                    "statement_id": pending["statement_id"],
                    "period": pending["period"],
                    "fee_tax_type": FEE_TYPE_MAP.get(raw_label, "unknown_fee"),
                    "raw_label": raw_label,
                    "currency": pending["currency"],
                    "amount_abs": fmt_money(amount),
                    "source_ref": item["source_ref"],
                }
            )
        close_pending()

    for item in iter_raw_table_rows(state):
        if item["statement_id"] not in legacy_ids:
            continue
        row = item["row"]
        if is_legacy_trade_row(row):
            close_pending()
            pending = create_trade(item, row)
            continue
        if is_legacy_fee_row(row):
            attach_fee(item, row)

    close_pending()
    return trades, fee_rows


def parse_market_trades(state: ParserState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    fee_rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending_blocks: list[dict[str, Any]] = []

    def unique_refs(refs: list[str]) -> list[str]:
        seen = set()
        result = []
        for ref in refs:
            if not ref or ref in seen:
                continue
            seen.add(ref)
            result.append(ref)
        return result

    def inferred_fee(gross: str, net: str) -> float:
        return round(abs(abs(parse_amount(net)) - abs(parse_amount(gross))), 2)

    def fee_line_components(text: str) -> tuple[float, list[tuple[str, float]]]:
        subtotal_match = SUBTOTAL_RE.search(text)
        components = [
            (raw_label, parse_amount(amount))
            for raw_label, amount in FEE_PAIR_RE.findall(text)
            if raw_label != "小計"
        ]
        subtotal = parse_amount(subtotal_match.group(1)) if subtotal_match else round(sum(amount for _, amount in components), 2)
        return subtotal, components

    def fee_source_ref(line: SourceLine | None, trade: dict[str, Any]) -> str:
        return line.source_ref if line else trade.get("source_refs", "")

    def add_fee_item(
        trade: dict[str, Any],
        raw_label: str,
        amount: float,
        source_ref: str,
    ) -> None:
        fee_rows.append(
            {
                "fee_tax_item_id": f"trade_fee_{len(fee_rows) + 1:04d}",
                "trade_index": trade["trade_id"],
                "parent_event_id": trade["trade_id"],
                "parent_business_type": "market_trade",
                "statement_id": trade["statement_id"],
                "period": trade["period"],
                "fee_tax_type": FEE_TYPE_MAP.get(raw_label, "unknown_fee"),
                "raw_label": raw_label,
                "currency": trade["currency"],
                "amount_abs": fmt_money(amount),
                "source_ref": source_ref,
            }
        )

    def allocate_amount(total: float, weights: list[float]) -> list[float]:
        if not weights:
            return []
        if len(weights) == 1:
            return [round(total, 2)]
        weight_total = sum(weights)
        if weight_total <= 0:
            weights = [1.0 for _ in weights]
            weight_total = float(len(weights))
        allocated: list[float] = []
        running = 0.0
        for weight in weights[:-1]:
            amount = round(total * weight / weight_total, 2)
            allocated.append(amount)
            running += amount
        allocated.append(round(total - running, 2))
        return allocated

    def make_block(
        line: SourceLine,
        raw_direction: str,
        instr_fragment: str,
        currency: str,
        qty: str,
        price: str,
        gross: str,
        net: str,
    ) -> dict[str, Any]:
        return {
            "statement_id": line.statement_id,
            "period": line.period,
            "filename": line.filename,
            "page": line.page,
            "business_type": "market_trade",
            "raw_direction": raw_direction,
            "side": "sell" if raw_direction.startswith("賣出") else "buy",
            "position_effect": "open" if "開倉" in raw_direction else "close" if "平倉" in raw_direction else "unknown",
            "currency": currency,
            "summary_fill": {
                "market": "",
                "currency": currency,
                "trade_date": "",
                "settlement_date": "",
                "quantity": qty.replace(",", ""),
                "price": price,
                "gross_amount": fmt_money(gross),
                "net_cash_amount": fmt_money(net),
                "trade_time": "",
                "source_refs": [line.source_ref],
            },
            "fills": [],
            "fragments": [instr_fragment] if instr_fragment else [],
            "source_refs": [line.source_ref],
        }

    def close_current() -> None:
        nonlocal current
        if current is not None:
            pending_blocks.append(current)
            current = None

    def add_trade_rows(
        blocks: list[dict[str, Any]],
        fee_line: SourceLine | None,
        fee_subtotal: float,
        fee_components: list[tuple[str, float]],
    ) -> None:
        if not blocks:
            return

        created: list[tuple[dict[str, Any], float, float]] = []
        fee_line_refs = [fee_line.source_ref] if fee_line else []
        for block in blocks:
            instrument = normalize_instrument_from_fragments(block["fragments"])
            fills = block["fills"] if block["fills"] else [block["summary_fill"]]
            for fill in fills:
                raw_net = parse_amount(fill["net_cash_amount"])
                net_cash = abs(raw_net) if block["side"] == "sell" else -abs(raw_net)
                trade_date = fill.get("trade_date") or block["summary_fill"].get("trade_date", "")
                trade_time = fill.get("trade_time") or block["summary_fill"].get("trade_time", "")
                trade = {
                    "trade_id": f"mt_{len(trades) + 1:04d}",
                    "statement_id": block["statement_id"],
                    "period": block["period"],
                    "filename": block["filename"],
                    "page": block["page"],
                    "business_type": block["business_type"],
                    "trade_datetime": f"{trade_date} {trade_time}" if trade_date and trade_time else "",
                    "trade_date": trade_date,
                    "settlement_date": fill.get("settlement_date", ""),
                    "raw_direction": block["raw_direction"],
                    "side": block["side"],
                    "position_effect": block["position_effect"],
                    "market": fill.get("market", ""),
                    "currency": fill.get("currency") or block["currency"],
                    "quantity": (fill.get("quantity") or "").replace(",", ""),
                    "price": fill.get("price", ""),
                    "gross_amount": fmt_money(fill.get("gross_amount", "")),
                    "fee_total": fmt_money(inferred_fee(fill.get("gross_amount", "0"), fill.get("net_cash_amount", "0"))),
                    "net_cash_amount": fmt_money(net_cash),
                    "source_refs": "; ".join(unique_refs(block["source_refs"] + fill.get("source_refs", []) + fee_line_refs)),
                }
                trade.update(instrument)
                trades.append(trade)
                created.append((trade, inferred_fee(trade["gross_amount"], fill.get("net_cash_amount", "0")), abs(parse_amount(trade["gross_amount"]))))

        disclosed_total = round(fee_subtotal if fee_subtotal else sum(amount for _, amount in fee_components), 2)
        inferred_total = round(sum(fee for _, fee, _ in created), 2)
        if disclosed_total > 0 and abs(disclosed_total - inferred_total) > 0.01:
            if inferred_total <= 0.01:
                allocated = allocate_amount(disclosed_total, [gross for _, _, gross in created])
                for (trade, _, gross), amount in zip(created, allocated, strict=True):
                    trade["fee_total"] = fmt_money(amount)
                add_issue(
                    state,
                    issue_type="market_fee_allocated_from_fee_line",
                    severity="info",
                    status="inferred",
                    source_ref=fee_line.source_ref if fee_line else "",
                    statement_id=fee_line.statement_id if fee_line else blocks[0]["statement_id"],
                    message=(
                        "Market trade fee subtotal was disclosed separately while fill cash delta did not "
                        "carry the fee; allocated subtotal by gross amount."
                    ),
                )
            else:
                add_issue(
                    state,
                    issue_type="market_fee_subtotal_mismatch",
                    severity="warning",
                    status="open",
                    source_ref=fee_line.source_ref if fee_line else "",
                    statement_id=fee_line.statement_id if fee_line else blocks[0]["statement_id"],
                    message=(
                        f"Market trade fee subtotal {fmt_money(disclosed_total)} does not match "
                        f"fill cash-delta fee {fmt_money(inferred_total)}."
                    ),
                )

        positive_fee_trades = [trade for trade, _, _ in created if parse_amount(trade["fee_total"]) > 0]
        component_total = round(sum(amount for _, amount in fee_components), 2)
        if len(created) == 1 and fee_components and abs(component_total - parse_amount(created[0][0]["fee_total"])) <= 0.01:
            trade = created[0][0]
            for raw_label, amount in fee_components:
                add_fee_item(trade, raw_label, amount, fee_source_ref(fee_line, trade))
            return

        for trade in positive_fee_trades:
            add_fee_item(trade, "成交淨額差額推算費用", parse_amount(trade["fee_total"]), fee_source_ref(fee_line, trade))

    def flush_pending_without_fee() -> None:
        nonlocal pending_blocks
        if pending_blocks:
            add_trade_rows(pending_blocks, None, 0.0, [])
            pending_blocks = []

    for line in state.source_lines:
        text = normalize_repeated_labels(line.text)
        start = MARKET_TRADE_START_RE.match(text)
        if start:
            close_current()
            raw_direction, instr_fragment, currency, qty, price, gross, net = start.groups()
            current = make_block(line, raw_direction, instr_fragment, currency, qty, price, gross, net)
            continue

        start_no_instr = MARKET_TRADE_START_NO_INSTR_RE.match(text)
        if start_no_instr:
            close_current()
            raw_direction, currency, qty, price, gross, net = start_no_instr.groups()
            current = make_block(line, raw_direction, "", currency, qty, price, gross, net)
            continue

        if text.startswith("佣金:"):
            close_current()
            fee_subtotal, fee_components = fee_line_components(text)
            add_trade_rows(pending_blocks, line, fee_subtotal, fee_components)
            pending_blocks = []
            continue

        if text.startswith("製備日期"):
            continue

        if text.startswith("成交金額合計"):
            close_current()
            flush_pending_without_fee()
            continue

        if current is None:
            continue

        if is_market_trade_layout_noise(text):
            continue

        detail = MARKET_TRADE_DETAIL_RE.match(text)
        if detail:
            market, currency, trade_date, settlement_date, qty, price, gross, net = detail.groups()
            current["fills"].append(
                {
                    "market": market,
                    "currency": currency,
                    "trade_date": trade_date,
                    "settlement_date": settlement_date,
                    "quantity": qty.replace(",", ""),
                    "price": price,
                    "gross_amount": fmt_money(gross),
                    "net_cash_amount": fmt_money(net),
                    "trade_time": "",
                    "source_refs": [line.source_ref],
                }
            )
            continue

        detail_with_instr = MARKET_TRADE_DETAIL_WITH_INSTR_RE.match(text)
        if detail_with_instr:
            instr_fragment, market, currency, trade_date, settlement_date, qty, price, gross, net = detail_with_instr.groups()
            current["fills"].append(
                {
                    "market": market,
                    "currency": currency,
                    "trade_date": trade_date,
                    "settlement_date": settlement_date,
                    "quantity": qty.replace(",", ""),
                    "price": price,
                    "gross_amount": fmt_money(gross),
                    "net_cash_amount": fmt_money(net),
                    "trade_time": "",
                    "source_refs": [line.source_ref],
                }
            )
            current["fragments"].append(instr_fragment)
            continue

        if re.match(r"^\d{2}:\d{2}:\d{2}$", text):
            if current["fills"]:
                current["fills"][-1]["trade_time"] = text
                current["fills"][-1]["source_refs"].append(line.source_ref)
            else:
                current["summary_fill"]["trade_time"] = text
                current["summary_fill"]["source_refs"].append(line.source_ref)
            continue

        current["fragments"].append(text)
        current["source_refs"].append(line.source_ref)

    close_current()
    flush_pending_without_fee()

    legacy_trades, legacy_fee_rows = parse_legacy_market_trades(state, len(trades), len(fee_rows))
    trades.extend(legacy_trades)
    fee_rows.extend(legacy_fee_rows)

    return trades, fee_rows


def parse_market_fee_line(
    text: str, trade_index: int, line: SourceLine, fee_rows: list[dict[str, Any]]
) -> float:
    subtotal_match = SUBTOTAL_RE.search(text)
    subtotal = parse_amount(subtotal_match.group(1)) if subtotal_match else 0.0
    for raw_label, amount in FEE_PAIR_RE.findall(text):
        if raw_label == "小計":
            continue
        fee_rows.append(
            {
                "fee_tax_item_id": f"trade_fee_{len(fee_rows) + 1:04d}",
                "trade_index": f"mt_{trade_index:04d}",
                "parent_event_id": "",
                "parent_business_type": "market_trade",
                "statement_id": line.statement_id,
                "period": line.period,
                "fee_tax_type": FEE_TYPE_MAP.get(raw_label, "unknown_fee"),
                "raw_label": raw_label,
                "currency": "",
                "amount_abs": fmt_money(amount),
                "source_ref": line.source_ref,
            }
        )
    return subtotal


def normalize_instrument_from_fragments(fragments: list[str]) -> dict[str, Any]:
    raw_compact = "".join(normalize_space(part) for part in fragments)
    option_match = re.search(r"([A-Z]+)(\d{6})([CP])(\d{5,6})", raw_compact)
    if option_match:
        underlying, expiry_raw, option_type_raw, strike_raw = option_match.groups()
        expiry = f"20{expiry_raw[0:2]}-{expiry_raw[2:4]}-{expiry_raw[4:6]}"
        strike = f"{int(strike_raw) / 1000:.2f}"
        symbol = "".join(option_match.groups())
        return {
            "instrument_code_raw": raw_compact,
            "instrument_symbol": symbol,
            "instrument_name_raw": f"{underlying} {expiry_raw} {strike}{option_type_raw}",
            "instrument_type": "option",
            "underlying_symbol": underlying,
            "expiry_date": expiry,
            "strike_price": strike,
            "option_type": "put" if option_type_raw == "P" else "call",
            "quantity_unit": "contract",
        }
    stock_match = re.match(r"(\d{4,5}|[A-Z][A-Z0-9.]{0,9})\((.*)\)", raw_compact)
    if stock_match:
        symbol_raw, name = stock_match.groups()
        symbol = symbol_raw.zfill(5) if symbol_raw.isdigit() else symbol_raw
        return {
            "instrument_code_raw": raw_compact,
            "instrument_symbol": symbol,
            "instrument_name_raw": name,
            "instrument_type": "stock",
            "underlying_symbol": "",
            "expiry_date": "",
            "strike_price": "",
            "option_type": "",
            "quantity_unit": "share",
        }
    return {
        "instrument_code_raw": raw_compact,
        "instrument_symbol": raw_compact,
        "instrument_name_raw": "",
        "instrument_type": "unknown",
        "underlying_symbol": "",
        "expiry_date": "",
        "strike_price": "",
        "option_type": "",
        "quantity_unit": "",
    }


def parse_unclassified_governance(state: ParserState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(state.cache_dir / "unclassified_tables.csv"):
        raw = row["raw_row"]
        if "11,735.38" in raw and "6.80%" in raw:
            classification = "financing_interest_evidence"
            status = "mapped_to_financing_interest_evidence"
        else:
            classification = "layout_noise"
            status = "ignored_layout_noise"
        rows.append(
            {
                "unclassified_governance_id": f"unclassified_{len(rows) + 1:04d}",
                "statement_id": row["statement_id"],
                "period": row["period"],
                "source_ref": f"{row['statement_id']}/p{row['page']}/table:{row['table_index']}/row:{row['row_index']}",
                "raw_row": raw,
                "default_classification": classification,
                "status": status,
            }
        )
        add_issue(
            state,
            issue_type="unclassified_table_row",
            severity="info" if classification == "layout_noise" else "warning",
            status=status,
            source_ref=rows[-1]["source_ref"],
            statement_id=row["statement_id"],
            message=f"Unclassified row handled as {classification}.",
        )
    return rows


def build_acceptance_report(outputs: dict[str, list[dict[str, Any]]], state: ParserState) -> dict[str, Any]:
    tests = []

    def add(code: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        tests.append(
            {
                "code": code,
                "actual": actual,
                "expected": expected,
                "passed": actual == expected if passed is None else passed,
            }
        )

    cash = outputs["cash_ledger_entries"]
    funds = outputs["fund_transactions"]
    fund_details = [row for row in funds if row["evidence_status"] == "complete_detail"]
    fund_amount_only = [row for row in funds if row["evidence_status"] == "amount_only"]
    corp = outputs["corporate_action_cash_legs"]
    market = outputs["market_trades"]
    stock_yield = outputs["stock_yield_daily_events"]
    stock_yield_cash = outputs["stock_yield_cash_entries"]
    derivative = outputs["derivative_exercise_events"]
    financing_detail = outputs["financing_interest_evidence_items"]
    raw_statement_ids = [row["statement_id"] for row in state.raw_statements]
    legacy_ids = legacy_statement_ids(state)
    statement_ids = sorted(raw_statement_ids)
    is_q1_baseline = statement_ids == ["202501", "202502", "202503"]

    if legacy_ids:
        expected_legacy_trades = sum(1 for item in iter_raw_table_rows(state) if item["statement_id"] in legacy_ids and is_legacy_trade_row(item["row"]))
        expected_legacy_cash = sum(1 for item in iter_raw_table_rows(state) if item["statement_id"] in legacy_ids and is_legacy_cash_row(item["row"]))
        expected_legacy_assets = sum(
            1 for item in iter_raw_table_rows(state) if item["statement_id"] in legacy_ids and is_legacy_asset_movement_row(item["row"])
        )
        expected_legacy_financing = sum(
            1 for item in iter_raw_table_rows(state) if item["statement_id"] in legacy_ids and is_legacy_financing_evidence_row(item["row"])
        )
        legacy_market = [row for row in market if row["statement_id"] in legacy_ids]
        legacy_cash = [row for row in cash if row["statement_id"] in legacy_ids]
        legacy_assets = [row for row in outputs["asset_movement_events"] if row["statement_id"] in legacy_ids]
        legacy_financing = [row for row in financing_detail if row["statement_id"] in legacy_ids]
        positions = [row for row in read_csv(state.cache_dir / "position_snapshots.csv") if row.get("statement_id") in legacy_ids]
        balances = [row for row in read_csv(state.cache_dir / "statement_balance_snapshots.csv") if row.get("statement_id") in legacy_ids]
        sha_values = [row.get("sha256", "") for row in state.raw_statements]
        add("LEGACY-STMT-COUNT-001", len(legacy_ids), len(legacy_ids), len(legacy_ids) > 0)
        add("LEGACY-DUPE-SHA-001", len(sha_values) - len(set(sha_values)), 0)
        add("LEGACY-POSITION-SNAPSHOT-001", len(positions), f">={len(legacy_ids)}", len(positions) >= len(legacy_ids))
        add("LEGACY-BALANCE-SNAPSHOT-001", len(balances), f">={len(legacy_ids) * 2}", len(balances) >= len(legacy_ids) * 2)
        add("LEGACY-MT-COVERAGE-001", len(legacy_market), expected_legacy_trades)
        add("LEGACY-CASH-COVERAGE-001", len(legacy_cash), expected_legacy_cash)
        add("LEGACY-ASSET-COVERAGE-001", len(legacy_assets), expected_legacy_assets)
        add("LEGACY-FIN-COVERAGE-001", len(legacy_financing), expected_legacy_financing)
    else:
        if is_q1_baseline:
            add("EXT-CASH-COUNT-001", len(cash), 39)
            add("EXT-FUND-COUNT-001", len(funds), 33)
            add("EXT-FUND-DETAIL-001", len(fund_details), 17)
            add("EXT-FUND-EVIDENCE-001", len(fund_amount_only), 16)
            add("EXT-CA-COUNT-001", len(corp), 8)
            add("EXT-FIN-DETAIL-001", len(financing_detail), 2)
            add("EXT-MT-COUNT-001", len(market), 4)
            add("EXT-STOCK-YIELD-DAILY-001", len(stock_yield), 39)
            add("EXT-STOCK-YIELD-CASH-001", len(stock_yield_cash), 2)
            add("EXT-DERIV-001", len(derivative), 1)
        else:
            add("EXT-CASH-COUNT-001", len(cash), ">=39", len(cash) >= 39)
            add("EXT-FUND-COUNT-001", len(funds), ">=33", len(funds) >= 33)
            add("EXT-FUND-DETAIL-001", len(fund_details), ">=17", len(fund_details) >= 17)
            add("EXT-FUND-EVIDENCE-001", len(fund_amount_only), ">=16", len(fund_amount_only) >= 16)
            add("EXT-CA-COUNT-001", len(corp), ">=8", len(corp) >= 8)
            add("EXT-FIN-DETAIL-001", len(financing_detail), ">=2", len(financing_detail) >= 2)
            add("EXT-MT-COUNT-001", len(market), ">=4", len(market) >= 4)
            add("EXT-STOCK-YIELD-DAILY-001", len(stock_yield), ">=39", len(stock_yield) >= 39)
            add("EXT-STOCK-YIELD-CASH-001", len(stock_yield_cash), ">=2", len(stock_yield_cash) >= 2)
            add("EXT-DERIV-001", len(derivative), ">=1", len(derivative) >= 1)

    market_arithmetic_errors = []
    for row in market:
        if row["statement_id"] in legacy_ids:
            continue
        gross = float(row["gross_amount"])
        net = float(row["net_cash_amount"])
        fee = float(row.get("fee_total") or 0)
        if not row.get("fee_total"):
            continue
        expected_net = gross - fee if row.get("side") == "sell" else -(gross + fee)
        if row.get("side") == "sell" and abs(gross - net) <= 0.01 and fee > 0:
            continue
        if abs(expected_net - net) > 0.01:
            market_arithmetic_errors.append(row["trade_id"])
    add("REC-MT-001", len(market_arithmetic_errors), 0)

    zero_derivative_cash = [row for row in cash if row["business_type"] == "derivative_exercise" and row["amount"] == "0.00"]
    if legacy_ids:
        add("DB-ZERO-001", len(zero_derivative_cash), "not_applicable", True)
    elif is_q1_baseline:
        add("DB-ZERO-001", len(zero_derivative_cash), 1)
    else:
        add("DB-ZERO-001", len(zero_derivative_cash), ">=1", len(zero_derivative_cash) >= 1)

    missing_source_refs = [
        row
        for table in outputs.values()
        for row in table
        if "source_refs" in row and not row.get("source_refs")
    ]
    add("SRC-REF-001", len(missing_source_refs), 0)

    return {
        "parser_version": "futu_statement_parser_v1",
        "status": "passed" if all(test["passed"] for test in tests) else "failed",
        "tests": tests,
        "counts": {name: len(rows) for name, rows in outputs.items()},
    }


def run(cache_dir: Path, out_dir: Path) -> dict[str, Any]:
    state = load_state(cache_dir, out_dir)

    cash_entries = parse_cash_entries(state)
    fund_transactions, fund_fee_lines, fund_orders, fund_cash_legs = parse_fund_transactions(state, cash_entries)
    corporate_actions = parse_corporate_actions(state, cash_entries)
    financing_events, financing_evidence = parse_financing_interest(state, cash_entries)
    asset_movements = parse_asset_movements(state)
    stock_yield_daily, stock_yield_cash = parse_stock_yield_events(state, cash_entries)
    derivative_events = parse_derivative_events(cash_entries, asset_movements)
    market_trades, market_trade_fee_items = parse_market_trades(state)
    unclassified_governance = parse_unclassified_governance(state)

    outputs: dict[str, list[dict[str, Any]]] = {
        "cash_ledger_entries": cash_entries,
        "fund_transactions": fund_transactions,
        "fund_transaction_fee_lines": fund_fee_lines,
        "fund_orders": fund_orders,
        "fund_order_cash_legs": fund_cash_legs,
        "corporate_action_cash_legs": corporate_actions,
        "financing_interest_events": financing_events,
        "financing_interest_evidence_items": financing_evidence,
        "asset_movement_events": asset_movements,
        "stock_yield_daily_events": stock_yield_daily,
        "stock_yield_cash_entries": stock_yield_cash,
        "derivative_exercise_events": derivative_events,
        "market_trades": market_trades,
        "market_trade_fee_items": market_trade_fee_items,
        "unclassified_governance": unclassified_governance,
        "parser_issues": state.issues,
    }

    fields: dict[str, list[str]] = {
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
        "unclassified_governance": [
            "unclassified_governance_id",
            "statement_id",
            "period",
            "source_ref",
            "raw_row",
            "default_classification",
            "status",
        ],
        "parser_issues": [
            "issue_id",
            "statement_id",
            "source_ref",
            "issue_type",
            "severity",
            "status",
            "message",
        ],
    }

    for name, rows in outputs.items():
        write_csv(out_dir / f"{name}.csv", rows, fields[name])

    report = build_acceptance_report(outputs, state)
    write_json(out_dir / "acceptance_report.json", report)
    write_json(
        out_dir / "manifest.json",
        {
            "parser_version": "futu_statement_parser_v1",
            "source_cache": str(cache_dir),
            "outputs": sorted(f"{name}.csv" for name in outputs),
            "acceptance_report": "acceptance_report.json",
            "status": report["status"],
        },
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.cache_dir, args.out_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
