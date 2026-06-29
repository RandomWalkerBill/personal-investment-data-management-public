#!/usr/bin/env python3
"""Lot / Allocation v1 CLI.

This calculator builds deterministic FIFO lots and allocations from the
validated Futu raw facts. It covers long stock/ETF positions, IPO allotments,
option contract lots, fund subscriptions/redemptions, and short stock lots.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "lot_allocation_schema_v1.sql"
DEFAULT_REPORT = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "lot-allocation-v1-report.md"
DEFAULT_EXPORT_DIR = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "lot-allocation-v1"

Q2 = Decimal("0.01")
Q6 = Decimal("0.000001")
IPO_ALLOTMENT_FEE_RATE = Decimal("0.010085")

DATE_RE = re.compile(r"(20\d{2})[/-](\d{2})[/-](\d{2})")
HK_CODE_AT_START_RE = re.compile(r"^\s*#?(\d{4,5})(?=\(|\b)")
HK_CODE_HASH_RE = re.compile(r"#(\d{4,5})")
US_SYMBOL_AT_START_RE = re.compile(r"^\s*([A-Z]{1,6})(?=\(|\b)")
OPTION_CODE_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d+", re.IGNORECASE)
OPTION_CODE_ANY_RE = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{3,7})", re.IGNORECASE)
FUND_CODE_RE = re.compile(r"(HK\d{10})", re.IGNORECASE)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value).replace(",", ""))


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_UP)


def q6(value: Decimal) -> Decimal:
    return value.quantize(Q6, rounding=ROUND_HALF_UP)


def dbnum(value: Decimal) -> str:
    return str(value)


def decimal_key(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_date(value: Any) -> str | None:
    raw = text(value)
    if not raw:
        return None
    match = DATE_RE.search(raw)
    if not match:
        return raw
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def extract_dates(*parts: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for match in DATE_RE.finditer(text(part)):
            normalized = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            if normalized not in seen:
                found.append(normalized)
                seen.add(normalized)
    return found


def period_start_date(period: str) -> str:
    return f"{period[:4]}-{period[4:6]}-01"


def clean_name(raw: str, code_or_symbol: str) -> str:
    value = text(raw)
    if not value:
        return ""
    paren = re.search(r"\(([^)]+)\)", value)
    if paren:
        return paren.group(1).strip()
    value = value.replace(code_or_symbol, "", 1).strip()
    value = re.split(r"保證金|FUTU OTC|SEHK|XNDQ|JNST|BATO|MCRY|EDGO|AMXO|EDGX|MXOP|XISX", value)[0]
    return value.strip(" ()")


@dataclass
class Instrument:
    key: str | None
    code: str | None
    name: str | None
    market: str | None
    inferred_type: str
    eligible_long_equity: bool
    reason: str = ""


def infer_instrument(
    *,
    code_raw: Any = None,
    symbol: Any = None,
    name_raw: Any = None,
    market: Any = None,
    currency: Any = None,
    instrument_type: Any = None,
) -> Instrument:
    raw_parts = [text(code_raw), text(symbol), text(name_raw)]
    raw_joined = " ".join(part for part in raw_parts if part)
    raw_type = text(instrument_type).lower()
    market_text = text(market)
    currency_text = text(currency).upper()

    first_token = raw_parts[1] or raw_parts[0]
    if raw_type == "option" or OPTION_CODE_RE.match(first_token) or OPTION_CODE_RE.match(raw_parts[0]):
        return Instrument(None, first_token or None, clean_name(raw_parts[2] or raw_parts[0], first_token), market_text, "option", False, "option_deferred")

    if raw_parts[0].upper().startswith("HK000") or raw_parts[1].upper().startswith("HK000"):
        return Instrument(None, raw_parts[0] or raw_parts[1], clean_name(raw_parts[0] or raw_parts[2], raw_parts[0]), market_text, "fund", False, "fund_deferred")

    hk_code: str | None = None
    for candidate in raw_parts:
        hash_match = HK_CODE_HASH_RE.search(candidate)
        start_match = HK_CODE_AT_START_RE.search(candidate)
        if hash_match:
            hk_code = hash_match.group(1)
            break
        if start_match:
            hk_code = start_match.group(1)
            break
    if hk_code:
        normalized = hk_code.zfill(5)
        name = clean_name(raw_parts[2] or raw_parts[0] or raw_parts[1], hk_code)
        return Instrument(
            key=f"HK:{normalized}",
            code=normalized,
            name=name or None,
            market="HK",
            inferred_type="stock_or_etf",
            eligible_long_equity=True,
        )

    symbol_match = US_SYMBOL_AT_START_RE.search(raw_parts[1] or raw_parts[0])
    if symbol_match and currency_text == "USD":
        us_symbol = symbol_match.group(1).upper()
        name = clean_name(raw_parts[2] or raw_parts[0] or raw_parts[1], us_symbol)
        return Instrument(
            key=f"US:{us_symbol}",
            code=us_symbol,
            name=name or None,
            market="US",
            inferred_type="stock_or_etf",
            eligible_long_equity=True,
        )

    return Instrument(None, raw_parts[0] or raw_parts[1] or None, raw_parts[2] or None, market_text or None, raw_type or "unknown", False, "unclassified")


@dataclass
class Lot:
    lot_id: str
    account_id: str
    instrument_key: str
    instrument_code: str | None
    instrument_name: str | None
    market: str | None
    currency: str
    source_type: str
    source_table: str
    source_pk: str
    source_ref: str | None
    open_date: str | None
    settlement_date: str | None
    original_quantity: Decimal
    remaining_quantity: Decimal
    remaining_cost: Decimal
    cost_basis_total: Decimal
    cost_basis_principal: Decimal
    cost_basis_fee: Decimal
    cost_basis_status: str
    cost_basis_source: str
    notes: str | None = None
    components: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unit_cost(self) -> Decimal:
        if self.original_quantity == 0:
            return Decimal("0")
        return self.cost_basis_total / self.original_quantity

    @property
    def lot_status(self) -> str:
        return "closed" if self.remaining_quantity == 0 else "open"


@dataclass
class CloseEvent:
    event_id: str
    event_table: str
    event_date: str | None
    settlement_date: str | None
    source_ref: str | None
    instrument_key: str
    instrument_code: str | None
    instrument_name: str | None
    currency: str
    quantity: Decimal
    proceeds: Decimal
    notes: str | None = None


@dataclass
class OptionContract:
    contract_key: str
    code: str
    underlying_symbol: str
    expiry_date: str
    strike_price: Decimal
    option_type: str
    market: str | None
    currency: str
    underlying_instrument_key: str | None = None


@dataclass
class OptionLot:
    option_lot_id: str
    account_id: str
    contract: OptionContract
    position_side: str
    source_type: str
    source_table: str
    source_pk: str
    source_ref: str | None
    open_date: str | None
    settlement_date: str | None
    original_contracts: Decimal
    remaining_contracts: Decimal
    opening_net_cash_amount: Decimal
    remaining_opening_cash_amount: Decimal
    opening_gross_amount: Decimal
    opening_fee_total: Decimal
    contract_multiplier: Decimal | None
    notes: str | None = None

    @property
    def premium_status(self) -> str:
        if self.remaining_contracts == 0:
            return "realized"
        if self.remaining_contracts == self.original_contracts:
            return "open"
        return "partial"


@dataclass
class OptionCloseEvent:
    event_id: str
    event_table: str
    event_date: str | None
    settlement_date: str | None
    source_ref: str | None
    contract: OptionContract
    expected_position_side: str | None
    close_event_type: str
    close_outcome: str
    contracts: Decimal
    closing_cash_amount: Decimal
    notes: str | None = None


@dataclass
class FundInstrument:
    key: str
    code: str
    name: str | None
    currency: str


@dataclass
class FundLot:
    fund_lot_id: str
    account_id: str
    fund: FundInstrument
    source_type: str
    source_table: str
    source_pk: str
    source_ref: str | None
    open_date: str | None
    original_units: Decimal
    remaining_units: Decimal
    cost_basis_total: Decimal
    remaining_cost: Decimal
    cost_basis_status: str
    cost_basis_source: str
    cash_source_status: str | None
    settlement_status: str = "settled"
    notes: str | None = None

    @property
    def unit_cost(self) -> Decimal:
        if self.original_units == 0:
            return Decimal("0")
        return self.cost_basis_total / self.original_units

    @property
    def lot_status(self) -> str:
        if self.settlement_status != "settled":
            return self.settlement_status
        return "closed" if self.remaining_units == 0 else "open"


@dataclass
class FundRedemptionEvent:
    order_id: str
    redemption_date: str | None
    source_ref: str | None
    fund: FundInstrument
    units: Decimal
    proceeds: Decimal
    cash_match_status: str | None
    notes: str | None = None


@dataclass
class ShortStockLot:
    short_lot_id: str
    account_id: str
    instrument_key: str
    instrument_code: str | None
    instrument_name: str | None
    market: str | None
    currency: str
    source_table: str
    source_pk: str
    source_ref: str | None
    open_date: str | None
    settlement_date: str | None
    original_quantity: Decimal
    remaining_quantity: Decimal
    opening_net_cash_amount: Decimal
    remaining_opening_cash_amount: Decimal
    opening_gross_amount: Decimal
    opening_fee_total: Decimal
    notes: str | None = None

    @property
    def lot_status(self) -> str:
        return "closed" if self.remaining_quantity == 0 else "open"


@dataclass
class ShortStockCloseEvent:
    event_id: str
    event_table: str
    event_date: str | None
    settlement_date: str | None
    source_ref: str | None
    instrument_key: str
    instrument_code: str | None
    instrument_name: str | None
    currency: str
    quantity: Decimal
    closing_cash_amount: Decimal
    notes: str | None = None


def infer_fund_instrument(*, code_raw: Any = None, name_raw: Any = None, currency: Any = None) -> FundInstrument | None:
    parts = [text(code_raw), text(name_raw)]
    code: str | None = None
    for part in parts:
        match = FUND_CODE_RE.search(part)
        if match:
            code = match.group(1).upper()
            break
    if code is None:
        return None

    raw_name = text(name_raw) or text(code_raw)
    name = FUND_CODE_RE.sub("", raw_name).strip()
    if name.startswith("(") and name.endswith(")"):
        name = name[1:-1].strip()
    name = name.strip(" ()")
    return FundInstrument(
        key=f"FUND:{code}",
        code=code,
        name=name or None,
        currency=text(currency).upper(),
    )


def parse_option_contract(
    *,
    code_raw: Any = None,
    symbol: Any = None,
    name_raw: Any = None,
    market: Any = None,
    currency: Any = None,
    underlying_symbol: Any = None,
    expiry_date: Any = None,
    strike_price: Any = None,
    option_type: Any = None,
) -> OptionContract | None:
    parts = [text(symbol), text(code_raw), text(name_raw), text(underlying_symbol)]
    match = None
    for part in parts:
        match = OPTION_CODE_ANY_RE.search(part)
        if match:
            break
    if not match:
        return None

    underlying = text(underlying_symbol).upper() or match.group(1).upper()
    yy = int(match.group(2)[:2])
    year = 2000 + yy
    expiry = normalize_date(expiry_date) or f"{year:04d}-{match.group(2)[2:4]}-{match.group(2)[4:6]}"
    type_code = match.group(3).upper()
    parsed_option_type = "call" if type_code == "C" else "put"
    final_option_type = text(option_type).lower() or parsed_option_type
    parsed_strike = Decimal(match.group(4)) / Decimal("1000")
    final_strike = dec(strike_price) if text(strike_price) else parsed_strike
    code = f"{match.group(1).upper()}{match.group(2)}{type_code}{match.group(4)}"
    currency_text = text(currency).upper()
    market_text = text(market)
    contract_key = f"OPT:{underlying}:{expiry}:{'C' if final_option_type == 'call' else 'P'}:{decimal_key(final_strike)}"
    return OptionContract(
        contract_key=contract_key,
        code=code,
        underlying_symbol=underlying,
        expiry_date=expiry,
        strike_price=final_strike,
        option_type=final_option_type,
        market=market_text or None,
        currency=currency_text,
        underlying_instrument_key=None,
    )


def infer_contract_multiplier(row: sqlite3.Row) -> Decimal | None:
    quantity = dec(row["quantity"])
    price = dec(row["price"])
    gross = abs(dec(row["gross_amount"]))
    if quantity == 0 or price == 0 or gross == 0:
        return None
    return q6(gross / (quantity * price))


class LotAllocator:
    def __init__(self, conn: sqlite3.Connection, allocation_run_id: str, import_run_id: str, account_id: str):
        self.conn = conn
        self.run_id = allocation_run_id
        self.import_run_id = import_run_id
        self.account_id = account_id
        self.lots: list[Lot] = []
        self.close_events: list[CloseEvent] = []
        self.allocations: list[dict[str, Any]] = []
        self.option_lots: list[OptionLot] = []
        self.option_close_events: list[OptionCloseEvent] = []
        self.option_allocations: list[dict[str, Any]] = []
        self.option_underlying_links: list[dict[str, Any]] = []
        self.fund_lots: list[FundLot] = []
        self.fund_redemption_events: list[FundRedemptionEvent] = []
        self.fund_allocations: list[dict[str, Any]] = []
        self.short_stock_lots: list[ShortStockLot] = []
        self.short_stock_close_events: list[ShortStockCloseEvent] = []
        self.short_stock_allocations: list[dict[str, Any]] = []
        self.validation_items: list[dict[str, Any]] = []
        self.sequence = {
            "lot": 0,
            "component": 0,
            "allocation": 0,
            "validation": 0,
            "option_lot": 0,
            "option_allocation": 0,
            "option_link": 0,
            "fund_lot": 0,
            "fund_allocation": 0,
            "short_lot": 0,
            "short_allocation": 0,
        }

    def next_id(self, prefix: str) -> str:
        self.sequence[prefix] += 1
        return f"{prefix}_{self.sequence[prefix]:04d}"

    def add_validation(
        self,
        *,
        check_code: str,
        status: str,
        severity: str,
        message: str,
        instrument_key: str | None = None,
        source_table: str | None = None,
        source_pk: str | None = None,
        expected_value: Decimal | None = None,
        actual_value: Decimal | None = None,
        diff_value: Decimal | None = None,
        notes: str | None = None,
    ) -> None:
        self.validation_items.append(
            {
                "validation_item_id": self.next_id("validation"),
                "check_code": check_code,
                "status": status,
                "severity": severity,
                "instrument_key": instrument_key,
                "source_table": source_table,
                "source_pk": source_pk,
                "expected_value": expected_value,
                "actual_value": actual_value,
                "diff_value": diff_value,
                "message": message,
                "notes": notes,
            }
        )

    def add_lot(self, lot: Lot) -> None:
        self.lots.append(lot)

    def add_component(
        self,
        lot: Lot,
        component_type: str,
        amount: Decimal,
        source_table: str | None,
        source_pk: str | None,
        source_ref: str | None,
        formula: str | None = None,
        notes: str | None = None,
    ) -> None:
        lot.components.append(
            {
                "component_id": self.next_id("component"),
                "lot_id": lot.lot_id,
                "component_type": component_type,
                "amount": amount,
                "currency": lot.currency,
                "source_table": source_table,
                "source_pk": source_pk,
                "source_ref": source_ref,
                "formula": formula,
                "notes": notes,
            }
        )

    def load_opening_lots(self) -> None:
        first_period = self.conn.execute(
            "SELECT MIN(period) FROM raw_statements WHERE import_run_id = ?",
            (self.import_run_id,),
        ).fetchone()[0]
        rows = self.conn.execute(
            """
            SELECT *
            FROM position_snapshots
            WHERE import_run_id = ?
              AND period = ?
              AND snapshot_type = 'opening'
              AND asset_category = 'stock_or_option'
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY code_name, page, table_index, row_index
            """,
            (self.import_run_id, first_period),
        ).fetchall()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if not inst.eligible_long_equity or not inst.key:
                self.add_validation(
                    check_code="opening_position_deferred",
                    status="skipped",
                    severity="info",
                    instrument_key=inst.key,
                    source_table="position_snapshots",
                    source_pk=f"{row['statement_id']}|{row['page']}|{row['table_index']}|{row['row_index']}",
                    message=f"期初持仓暂不进入 Lot v1：{row['code_name']}",
                    notes=inst.reason,
                )
                continue
            quantity = dec(row["quantity"])
            market_value = q2(dec(row["market_value"]))
            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=inst.key,
                instrument_code=inst.code,
                instrument_name=inst.name,
                market=inst.market,
                currency=text(row["currency"]).upper(),
                source_type="opening_position",
                source_table="position_snapshots",
                source_pk=f"{row['statement_id']}|{row['page']}|{row['table_index']}|{row['row_index']}",
                source_ref=None,
                open_date=period_start_date(first_period),
                settlement_date=None,
                original_quantity=quantity,
                remaining_quantity=quantity,
                remaining_cost=market_value,
                cost_basis_total=market_value,
                cost_basis_principal=market_value,
                cost_basis_fee=Decimal("0"),
                cost_basis_status="provisional",
                cost_basis_source="first_statement_opening_market_value",
                notes="期初临时 lot；历史真实成本待补，当前用于 2025 年内 FIFO 与期初市值口径收益。",
            )
            self.add_component(lot, "opening_market_value", market_value, "position_snapshots", lot.source_pk, None)
            self.add_lot(lot)

    def handling_fee_rows_for_ipo(self, hk_code: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT cash_entry_id, amount, source_refs
            FROM cash_ledger_entries
            WHERE import_run_id = ?
              AND business_type = 'ipo_subscription'
              AND cash_leg_type = 'application_handling_fee'
              AND description LIKE ?
            ORDER BY period, event_date, cash_entry_id
            """,
            (self.import_run_id, f"%#{hk_code}%"),
        ).fetchall()

    def load_ipo_allotment_lots(self) -> None:
        rows = self.conn.execute(
            """
            SELECT *
            FROM asset_movement_events
            WHERE import_run_id = ?
              AND business_type = 'ipo_subscription'
              AND asset_movement_type = 'allotment'
              AND quantity IS NOT NULL
              AND quantity > 0
              AND amount IS NOT NULL
              AND amount > 0
            ORDER BY event_date, asset_movement_id
            """,
            (self.import_run_id,),
        ).fetchall()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["description_raw"] or row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                market="HK",
                currency=row["currency"],
            )
            if not inst.eligible_long_equity or not inst.key or not inst.code:
                self.add_validation(
                    check_code="ipo_allotment_unclassified",
                    status="failed",
                    severity="error",
                    source_table="asset_movement_events",
                    source_pk=row["asset_movement_id"],
                    message=f"IPO 中签配发无法识别标的：{row['description_raw']}",
                )
                continue

            quantity = dec(row["quantity"])
            principal = q2(dec(row["amount"]))
            handling_fee_rows = self.handling_fee_rows_for_ipo(inst.code)
            handling_fee = q2(sum((abs(dec(fee_row["amount"])) for fee_row in handling_fee_rows), Decimal("0")))
            hidden_fee = q2(principal * IPO_ALLOTMENT_FEE_RATE)
            total_cost = q2(principal + handling_fee + hidden_fee)
            event_date = normalize_date(row["event_date"]) or (extract_dates(row["description_raw"])[0] if extract_dates(row["description_raw"]) else None)

            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=inst.key,
                instrument_code=inst.code,
                instrument_name=inst.name,
                market=inst.market,
                currency=text(row["currency"]).upper(),
                source_type="ipo_allotment",
                source_table="asset_movement_events",
                source_pk=row["asset_movement_id"],
                source_ref=row["source_ref"],
                open_date=event_date,
                settlement_date=None,
                original_quantity=quantity,
                remaining_quantity=quantity,
                remaining_cost=total_cost,
                cost_basis_total=total_cost,
                cost_basis_principal=principal,
                cost_basis_fee=q2(handling_fee + hidden_fee),
                cost_basis_status="final",
                cost_basis_source="ipo_allotment_amount_plus_explicit_handling_fee_plus_formula_fee",
                notes="IPO 中签 lot；融资利息按期间费用保留，未分摊到 lot。",
            )
            self.add_component(lot, "ipo_allotment_principal", principal, "asset_movement_events", row["asset_movement_id"], row["source_ref"])
            if handling_fee:
                self.add_component(
                    lot,
                    "application_handling_fee",
                    handling_fee,
                    "cash_ledger_entries",
                    ",".join(fee_row["cash_entry_id"] for fee_row in handling_fee_rows),
                    ",".join(text(fee_row["source_refs"]) for fee_row in handling_fee_rows if text(fee_row["source_refs"])),
                )
            else:
                self.add_validation(
                    check_code="ipo_handling_fee_absent",
                    status="passed",
                    severity="info",
                    instrument_key=inst.key,
                    source_table="asset_movement_events",
                    source_pk=row["asset_movement_id"],
                    expected_value=Decimal("0"),
                    actual_value=Decimal("0"),
                    diff_value=Decimal("0"),
                    message=f"{inst.key} 中签 lot 未匹配到显式 HKD 100 申购费；按原始现金事实保留为 0。",
                )
            self.add_component(
                lot,
                "ipo_allotment_fee_or_levy",
                hidden_fee,
                "asset_movement_events",
                row["asset_movement_id"],
                row["source_ref"],
                formula="allotment_amount * 0.010085",
                notes="富途 IPO 中签 1% 手续费 + 约 0.0085% 小额市场/政府费用的合并口径。",
            )
            self.add_lot(lot)

    def normalized_trade_dates(self, row: sqlite3.Row) -> tuple[str | None, str | None, str | None]:
        dates = extract_dates(row["trade_date"], row["settlement_date"], row["instrument_code_raw"], row["instrument_symbol"])
        trade_date = normalize_date(row["trade_date"]) or (dates[0] if dates else None)
        settlement_date = normalize_date(row["settlement_date"]) or (dates[1] if len(dates) > 1 else None)
        note = None
        if not row["trade_date"] and trade_date:
            note = "交易日期由原始交易文本补抽。"
        return trade_date, settlement_date, note

    def load_market_trade_lots_and_closes(self) -> None:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_trades
            WHERE import_run_id = ?
            ORDER BY period, COALESCE(trade_date, ''), trade_id
            """,
            (self.import_run_id,),
        ).fetchall()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_symbol"],
                name_raw=row["instrument_name_raw"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["instrument_type"],
            )
            source_pk = row["trade_id"]
            side = text(row["side"]).lower()
            position_effect = text(row["position_effect"]).lower()
            trade_date, settlement_date, note = self.normalized_trade_dates(row)
            if not inst.eligible_long_equity or not inst.key:
                if inst.inferred_type == "option" or parse_option_contract(
                    code_raw=row["instrument_code_raw"],
                    symbol=row["instrument_symbol"],
                    name_raw=row["instrument_name_raw"],
                    market=row["market"],
                    currency=row["currency"],
                    underlying_symbol=row["underlying_symbol"],
                    expiry_date=row["expiry_date"],
                    strike_price=row["strike_price"],
                    option_type=row["option_type"],
                ):
                    continue
                self.add_validation(
                    check_code="market_trade_deferred",
                    status="skipped",
                    severity="info",
                    source_table="market_trades",
                    source_pk=source_pk,
                    message=f"市场交易暂不进入 Lot v1：{row['instrument_code_raw']} {side}/{position_effect}",
                    notes=inst.reason or inst.inferred_type,
                )
                continue

            quantity = dec(row["quantity"])
            if side == "buy" and position_effect == "open":
                net_cash = abs(dec(row["net_cash_amount"]))
                gross = abs(dec(row["gross_amount"]))
                fee = q2(net_cash - gross)
                if fee < 0:
                    fee = abs(dec(row["fee_total"]))
                total_cost = q2(net_cash)
                lot = Lot(
                    lot_id=self.next_id("lot"),
                    account_id=self.account_id,
                    instrument_key=inst.key,
                    instrument_code=inst.code,
                    instrument_name=inst.name,
                    market=inst.market,
                    currency=text(row["currency"]).upper(),
                    source_type="market_buy",
                    source_table="market_trades",
                    source_pk=source_pk,
                    source_ref=row["source_refs"],
                    open_date=trade_date,
                    settlement_date=settlement_date,
                    original_quantity=quantity,
                    remaining_quantity=quantity,
                    remaining_cost=total_cost,
                    cost_basis_total=total_cost,
                    cost_basis_principal=q2(gross),
                    cost_basis_fee=q2(fee),
                    cost_basis_status="final",
                    cost_basis_source="market_trade_net_cash_amount",
                    notes=note,
                )
                self.add_component(lot, "market_buy_gross_amount", q2(gross), "market_trades", source_pk, row["source_refs"])
                if fee:
                    self.add_component(lot, "market_buy_fee_tax_total", q2(fee), "market_trade_fee_items", source_pk, row["source_refs"])
                self.add_lot(lot)
            elif side == "sell" and position_effect == "close":
                self.close_events.append(
                    CloseEvent(
                        event_id=source_pk,
                        event_table="market_trades",
                        event_date=trade_date,
                        settlement_date=settlement_date,
                        source_ref=row["source_refs"],
                        instrument_key=inst.key,
                        instrument_code=inst.code,
                        instrument_name=inst.name,
                        currency=text(row["currency"]).upper(),
                        quantity=quantity,
                        proceeds=q2(dec(row["net_cash_amount"])),
                        notes=note,
                    )
                )
            elif side == "sell" and position_effect == "open":
                net_cash = q2(dec(row["net_cash_amount"]))
                gross = q2(abs(dec(row["gross_amount"])))
                fee = q2(gross - net_cash)
                if fee < 0:
                    fee = q2(abs(dec(row["fee_total"])))
                self.short_stock_lots.append(
                    ShortStockLot(
                        short_lot_id=self.next_id("short_lot"),
                        account_id=self.account_id,
                        instrument_key=inst.key,
                        instrument_code=inst.code,
                        instrument_name=inst.name,
                        market=inst.market,
                        currency=text(row["currency"]).upper(),
                        source_table="market_trades",
                        source_pk=source_pk,
                        source_ref=row["source_refs"],
                        open_date=trade_date,
                        settlement_date=settlement_date,
                        original_quantity=quantity,
                        remaining_quantity=quantity,
                        opening_net_cash_amount=net_cash,
                        remaining_opening_cash_amount=net_cash,
                        opening_gross_amount=gross,
                        opening_fee_total=fee,
                        notes=note,
                    )
                )
            elif side == "buy" and position_effect == "close":
                self.short_stock_close_events.append(
                    ShortStockCloseEvent(
                        event_id=source_pk,
                        event_table="market_trades",
                        event_date=trade_date,
                        settlement_date=settlement_date,
                        source_ref=row["source_refs"],
                        instrument_key=inst.key,
                        instrument_code=inst.code,
                        instrument_name=inst.name,
                        currency=text(row["currency"]).upper(),
                        quantity=quantity,
                        closing_cash_amount=q2(dec(row["net_cash_amount"])),
                        notes=note,
                    )
                )
            else:
                self.add_validation(
                    check_code="market_trade_direction_unhandled",
                    status="skipped",
                    severity="warning",
                    instrument_key=inst.key,
                    source_table="market_trades",
                    source_pk=source_pk,
                    message=f"市场交易方向暂无法进入 Lot v1：{inst.key} {side}/{position_effect} qty={quantity}",
                    notes="需要新样本确认该方向的业务含义。",
                )

    def load_option_lots_and_closes(self) -> None:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_trades
            WHERE import_run_id = ?
            ORDER BY period, COALESCE(trade_date, ''), trade_id
            """,
            (self.import_run_id,),
        ).fetchall()
        for row in rows:
            contract = parse_option_contract(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_symbol"],
                name_raw=row["instrument_name_raw"],
                market=row["market"],
                currency=row["currency"],
                underlying_symbol=row["underlying_symbol"],
                expiry_date=row["expiry_date"],
                strike_price=row["strike_price"],
                option_type=row["option_type"],
            )
            if contract is None:
                continue

            side = text(row["side"]).lower()
            position_effect = text(row["position_effect"]).lower()
            trade_date, settlement_date, note = self.normalized_trade_dates(row)
            quantity = dec(row["quantity"])
            net_cash = q2(dec(row["net_cash_amount"]))
            gross = q2(abs(dec(row["gross_amount"])))
            fee = q2(abs(dec(row["fee_total"])))
            multiplier = infer_contract_multiplier(row)

            if side == "sell" and position_effect == "open":
                self.option_lots.append(
                    OptionLot(
                        option_lot_id=self.next_id("option_lot"),
                        account_id=self.account_id,
                        contract=contract,
                        position_side="short",
                        source_type="option_sell_open",
                        source_table="market_trades",
                        source_pk=row["trade_id"],
                        source_ref=row["source_refs"],
                        open_date=trade_date,
                        settlement_date=settlement_date,
                        original_contracts=quantity,
                        remaining_contracts=quantity,
                        opening_net_cash_amount=net_cash,
                        remaining_opening_cash_amount=net_cash,
                        opening_gross_amount=gross,
                        opening_fee_total=fee,
                        contract_multiplier=multiplier,
                        notes=note,
                    )
                )
            elif side == "buy" and position_effect == "open":
                self.option_lots.append(
                    OptionLot(
                        option_lot_id=self.next_id("option_lot"),
                        account_id=self.account_id,
                        contract=contract,
                        position_side="long",
                        source_type="option_buy_open",
                        source_table="market_trades",
                        source_pk=row["trade_id"],
                        source_ref=row["source_refs"],
                        open_date=trade_date,
                        settlement_date=settlement_date,
                        original_contracts=quantity,
                        remaining_contracts=quantity,
                        opening_net_cash_amount=net_cash,
                        remaining_opening_cash_amount=net_cash,
                        opening_gross_amount=gross,
                        opening_fee_total=fee,
                        contract_multiplier=multiplier,
                        notes=note,
                    )
                )
            elif side == "buy" and position_effect == "close":
                self.option_close_events.append(
                    OptionCloseEvent(
                        event_id=row["trade_id"],
                        event_table="market_trades",
                        event_date=trade_date,
                        settlement_date=settlement_date,
                        source_ref=row["source_refs"],
                        contract=contract,
                        expected_position_side="short",
                        close_event_type="option_trade_close",
                        close_outcome="closed_by_buy_close",
                        contracts=quantity,
                        closing_cash_amount=net_cash,
                        notes=note,
                    )
                )
            elif side == "sell" and position_effect == "close":
                self.option_close_events.append(
                    OptionCloseEvent(
                        event_id=row["trade_id"],
                        event_table="market_trades",
                        event_date=trade_date,
                        settlement_date=settlement_date,
                        source_ref=row["source_refs"],
                        contract=contract,
                        expected_position_side="long",
                        close_event_type="option_trade_close",
                        close_outcome="closed_by_sell_close",
                        contracts=quantity,
                        closing_cash_amount=net_cash,
                        notes=note,
                    )
                )
            else:
                self.add_validation(
                    check_code="option_trade_direction_unhandled",
                    status="failed",
                    severity="error",
                    instrument_key=contract.contract_key,
                    source_table="market_trades",
                    source_pk=row["trade_id"],
                    message=f"期权交易方向暂无法处理：{contract.code} {side}/{position_effect}",
                )

    def load_option_exercise_events(self) -> None:
        rows = self.conn.execute(
            """
            SELECT *
            FROM asset_movement_events
            WHERE import_run_id = ?
              AND asset_movement_type = 'option_expiry_close'
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY event_date, asset_movement_id
            """,
            (self.import_run_id,),
        ).fetchall()
        for row in rows:
            contract = parse_option_contract(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                name_raw=row["description_raw"],
                market=None,
                currency=row["currency"],
            )
            if contract is None:
                self.add_validation(
                    check_code="option_exercise_unclassified",
                    status="failed",
                    severity="error",
                    source_table="asset_movement_events",
                    source_pk=row["asset_movement_id"],
                    message=f"期权到期/行权事件无法识别合约：{row['description_raw']}",
                )
                continue
            raw_description = text(row["description_raw"]).upper()
            if "ASS" in raw_description:
                outcome = "assigned_or_exercised"
            elif "EXP" in raw_description:
                outcome = "expired_worthless"
            else:
                outcome = "exercise_or_assignment_unknown"
            self.option_close_events.append(
                OptionCloseEvent(
                    event_id=row["asset_movement_id"],
                    event_table="asset_movement_events",
                    event_date=normalize_date(row["event_date"]),
                    settlement_date=None,
                    source_ref=row["source_ref"],
                    contract=contract,
                    expected_position_side=None,
                    close_event_type="option_expiry_or_assignment",
                    close_outcome=outcome,
                    contracts=dec(row["quantity"]),
                    closing_cash_amount=q2(dec(row["amount"])),
                    notes=row["description_raw"],
                )
            )

    def find_underlying_delivery_link(
        self,
        *,
        allocation_id: str,
        event: OptionCloseEvent,
        lot: OptionLot,
        contracts_allocated: Decimal,
    ) -> None:
        if lot.contract_multiplier is None:
            self.add_validation(
                check_code="option_assignment_underlying_link_missing",
                status="skipped",
                severity="warning",
                instrument_key=event.contract.contract_key,
                source_table=event.event_table,
                source_pk=event.event_id,
                message=f"{event.contract.code} 指派/行权缺少合约乘数，无法匹配底层交割。",
            )
            return

        expected_quantity = q6(contracts_allocated * lot.contract_multiplier)
        if event.contract.option_type == "call":
            expected_side = "sell" if lot.position_side == "short" else "buy"
        else:
            expected_side = "buy" if lot.position_side == "short" else "sell"
        expected_effect = "close" if expected_side == "sell" else "open"

        candidate_rows = self.conn.execute(
            """
            SELECT *
            FROM market_trades
            WHERE import_run_id = ?
              AND REPLACE(trade_date, '/', '-') = ?
              AND side = ?
              AND position_effect = ?
              AND currency = ?
            ORDER BY trade_id
            """,
            (self.import_run_id, event.event_date, expected_side, expected_effect, event.contract.currency),
        ).fetchall()
        matched: sqlite3.Row | None = None
        matched_inst: Instrument | None = None
        for row in candidate_rows:
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_symbol"],
                name_raw=row["instrument_name_raw"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["instrument_type"],
            )
            if not inst.eligible_long_equity or not inst.key:
                continue
            row_qty = q6(dec(row["quantity"]))
            row_price = dec(row["price"]) if text(row["price"]) else q6(abs(dec(row["gross_amount"])) / row_qty)
            if abs(row_qty - expected_quantity) <= Q6 and abs(row_price - event.contract.strike_price) <= Decimal("0.01"):
                matched = row
                matched_inst = inst
                break

        if matched is None or matched_inst is None:
            self.add_validation(
                check_code="option_assignment_underlying_link_missing",
                status="skipped",
                severity="warning",
                instrument_key=event.contract.contract_key,
                source_table=event.event_table,
                source_pk=event.event_id,
                expected_value=expected_quantity,
                message=f"{event.contract.code} 指派/行权未找到同日、同数量、同执行价的底层交割交易。",
            )
            return

        self.option_underlying_links.append(
            {
                "link_id": self.next_id("option_link"),
                "option_allocation_id": allocation_id,
                "option_lot_id": lot.option_lot_id,
                "option_contract_key": event.contract.contract_key,
                "link_type": "assignment_underlying_delivery",
                "underlying_event_table": "market_trades",
                "underlying_event_id": matched["trade_id"],
                "underlying_instrument_key": matched_inst.key,
                "underlying_quantity": expected_quantity,
                "strike_price": event.contract.strike_price,
                "underlying_gross_amount": q2(abs(dec(matched["gross_amount"]))),
                "confidence": "inferred_same_date_qty_strike",
                "notes": f"{event.contract.code} -> {matched_inst.key} {matched['trade_id']}",
            }
        )
        self.add_validation(
            check_code="option_assignment_underlying_linked",
            status="passed",
            severity="info",
            instrument_key=event.contract.contract_key,
            source_table=event.event_table,
            source_pk=event.event_id,
            expected_value=expected_quantity,
            actual_value=q6(dec(matched["quantity"])),
            diff_value=Decimal("0"),
            message=f"{event.contract.code} 指派/行权已链接底层交易 {matched['trade_id']}。",
        )

    def allocate_options_fifo(self) -> None:
        self.option_lots.sort(key=lambda lot: (lot.contract.contract_key, lot.position_side, lot.open_date or "", lot.option_lot_id))
        self.option_close_events.sort(key=lambda event: (event.event_date or "", event.event_id))
        lots_by_contract: dict[str, list[OptionLot]] = {}
        for lot in self.option_lots:
            lots_by_contract.setdefault(lot.contract.contract_key, []).append(lot)

        for event in self.option_close_events:
            remaining_contracts = event.contracts
            remaining_close_cash = event.closing_cash_amount
            candidate_lots = lots_by_contract.get(event.contract.contract_key, [])
            for lot in candidate_lots:
                if remaining_contracts <= 0:
                    break
                if event.expected_position_side and lot.position_side != event.expected_position_side:
                    continue
                if lot.remaining_contracts <= 0:
                    continue
                if event.event_date and lot.open_date and lot.open_date > event.event_date:
                    continue
                take_contracts = min(lot.remaining_contracts, remaining_contracts)
                is_last_piece_for_event = take_contracts == remaining_contracts
                is_closing_lot = take_contracts == lot.remaining_contracts
                if is_last_piece_for_event:
                    closing_cash_allocated = q2(remaining_close_cash)
                else:
                    closing_cash_allocated = q2(event.closing_cash_amount * take_contracts / event.contracts)
                if is_closing_lot:
                    opening_cash_allocated = q2(lot.remaining_opening_cash_amount)
                else:
                    opening_cash_allocated = q2(lot.remaining_opening_cash_amount * take_contracts / lot.remaining_contracts)
                realized_pnl = q2(opening_cash_allocated + closing_cash_allocated)
                allocation_id = self.next_id("option_allocation")
                self.option_allocations.append(
                    {
                        "option_allocation_id": allocation_id,
                        "option_lot_id": lot.option_lot_id,
                        "option_contract_key": event.contract.contract_key,
                        "option_code": event.contract.code,
                        "underlying_symbol": event.contract.underlying_symbol,
                        "underlying_instrument_key": event.contract.underlying_instrument_key,
                        "expiry_date": event.contract.expiry_date,
                        "strike_price": event.contract.strike_price,
                        "option_type": event.contract.option_type,
                        "currency": event.contract.currency,
                        "position_side": lot.position_side,
                        "close_event_type": event.close_event_type,
                        "close_outcome": event.close_outcome,
                        "close_event_table": event.event_table,
                        "close_event_id": event.event_id,
                        "close_event_date": event.event_date,
                        "close_settlement_date": event.settlement_date,
                        "close_source_ref": event.source_ref,
                        "contracts_allocated": take_contracts,
                        "opening_cash_allocated": opening_cash_allocated,
                        "closing_cash_allocated": closing_cash_allocated,
                        "realized_pnl": realized_pnl,
                        "pnl_status": "final",
                        "notes": event.notes,
                    }
                )
                if event.close_outcome == "assigned_or_exercised":
                    self.find_underlying_delivery_link(
                        allocation_id=allocation_id,
                        event=event,
                        lot=lot,
                        contracts_allocated=take_contracts,
                    )
                lot.remaining_contracts = q6(lot.remaining_contracts - take_contracts)
                lot.remaining_opening_cash_amount = q2(lot.remaining_opening_cash_amount - opening_cash_allocated)
                remaining_contracts = q6(remaining_contracts - take_contracts)
                remaining_close_cash = q2(remaining_close_cash - closing_cash_allocated)

            if abs(remaining_contracts) <= Q6:
                self.add_validation(
                    check_code="option_close_quantity_allocated",
                    status="passed",
                    severity="info",
                    instrument_key=event.contract.contract_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.contracts,
                    actual_value=event.contracts,
                    diff_value=Decimal("0"),
                    message=f"{event.event_id} 期权合约数量已完整 FIFO 分配。",
                )
            else:
                self.add_validation(
                    check_code="option_insufficient_lot",
                    status="failed",
                    severity="error",
                    instrument_key=event.contract.contract_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.contracts,
                    actual_value=q6(event.contracts - remaining_contracts),
                    diff_value=remaining_contracts,
                    message=f"{event.event_id} 可用期权 lot 不足，剩余未分配合约数 {remaining_contracts}。",
                )

    def validate_option_remaining_positions(self) -> None:
        latest_period = self.conn.execute(
            "SELECT MAX(period) FROM raw_statements WHERE import_run_id = ?",
            (self.import_run_id,),
        ).fetchone()[0]
        expected: dict[str, Decimal] = {}
        rows = self.conn.execute(
            """
            SELECT *
            FROM position_snapshots
            WHERE import_run_id = ?
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category = 'stock_or_option'
              AND quantity IS NOT NULL
            """,
            (self.import_run_id, latest_period),
        ).fetchall()
        for row in rows:
            contract = parse_option_contract(
                code_raw=row["code_name"],
                symbol=row["code_name"],
                market=row["market"],
                currency=row["currency"],
            )
            if contract is None:
                continue
            expected[contract.contract_key] = expected.get(contract.contract_key, Decimal("0")) + dec(row["quantity"])

        actual: dict[str, Decimal] = {}
        for lot in self.option_lots:
            sign = Decimal("-1") if lot.position_side == "short" else Decimal("1")
            actual[lot.contract.contract_key] = actual.get(lot.contract.contract_key, Decimal("0")) + sign * lot.remaining_contracts

        for key in sorted(set(expected) | set(actual)):
            expected_qty = q6(expected.get(key, Decimal("0")))
            actual_qty = q6(actual.get(key, Decimal("0")))
            diff = q6(actual_qty - expected_qty)
            status = "passed" if abs(diff) <= Q6 else "failed"
            severity = "info" if status == "passed" else "error"
            self.add_validation(
                check_code="option_ending_position_quantity_match",
                status=status,
                severity=severity,
                instrument_key=key,
                source_table="position_snapshots",
                source_pk=latest_period,
                expected_value=expected_qty,
                actual_value=actual_qty,
                diff_value=diff,
                message=f"{key} 期权 lot 剩余数量与 {latest_period} 期末持仓{'一致' if status == 'passed' else '不一致'}。",
            )

        negative_lots = [lot for lot in self.option_lots if lot.remaining_contracts < 0]
        if negative_lots:
            for lot in negative_lots:
                self.add_validation(
                    check_code="option_lot_remaining_nonnegative",
                    status="failed",
                    severity="error",
                    instrument_key=lot.contract.contract_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_contracts,
                    message=f"{lot.option_lot_id} 出现负剩余合约数量。",
                )
        else:
            self.add_validation(
                check_code="option_lot_remaining_nonnegative",
                status="passed",
                severity="info",
                message="所有期权 lot 剩余合约数量均非负。",
            )

    def load_fund_lots_and_redemptions(self) -> None:
        first_period = self.conn.execute(
            "SELECT MIN(period) FROM raw_statements WHERE import_run_id = ?",
            (self.import_run_id,),
        ).fetchone()[0]
        opening_rows = self.conn.execute(
            """
            SELECT *
            FROM position_snapshots
            WHERE import_run_id = ?
              AND period = ?
              AND snapshot_type = 'opening'
              AND asset_category = 'fund'
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY code_name, page, table_index, row_index
            """,
            (self.import_run_id, first_period),
        ).fetchall()
        for row in opening_rows:
            fund = infer_fund_instrument(code_raw=row["code_name"], currency=row["currency"])
            source_pk = f"{row['statement_id']}|{row['page']}|{row['table_index']}|{row['row_index']}"
            if fund is None:
                self.add_validation(
                    check_code="fund_opening_unclassified",
                    status="failed",
                    severity="error",
                    source_table="position_snapshots",
                    source_pk=source_pk,
                    message=f"基金期初持仓无法识别代码：{row['code_name']}",
                )
                continue
            units = dec(row["quantity"])
            nav_cost = q2(units * dec(row["price"])) if text(row["price"]) else q2(dec(row["market_value"]) - dec(row["pending_amount"]))
            lot = FundLot(
                fund_lot_id=self.next_id("fund_lot"),
                account_id=self.account_id,
                fund=fund,
                source_type="opening_position",
                source_table="position_snapshots",
                source_pk=source_pk,
                source_ref=None,
                open_date=period_start_date(first_period),
                original_units=units,
                remaining_units=units,
                cost_basis_total=nav_cost,
                remaining_cost=nav_cost,
                cost_basis_status="provisional",
                cost_basis_source="first_statement_opening_fund_nav_value",
                cash_source_status=None,
                notes="基金期初临时 lot；按份额 * 期初净值估算成本，不包含 pending amount。",
            )
            self.fund_lots.append(lot)

        order_rows = self.conn.execute(
            """
            SELECT *
            FROM fund_orders
            WHERE import_run_id = ?
            ORDER BY period, COALESCE(trade_date, order_date, ''), fund_order_id
            """,
            (self.import_run_id,),
        ).fetchall()
        for row in order_rows:
            fund = infer_fund_instrument(
                code_raw=row["instrument_code"],
                name_raw=row["instrument_name_raw"],
                currency=row["currency"],
            )
            if fund is None:
                self.add_validation(
                    check_code="fund_order_unclassified",
                    status="failed",
                    severity="error",
                    source_table="fund_orders",
                    source_pk=row["fund_order_id"],
                    message=f"基金订单无法识别代码：{row['instrument_code']} {row['instrument_name_raw']}",
                )
                continue

            order_type = text(row["fund_order_type"]).lower()
            units = dec(row["quantity"]) if text(row["quantity"]) else Decimal("0")
            amount = q2(dec(row["fund_amount_abs"]))
            trade_date = normalize_date(row["trade_date"]) or normalize_date(row["order_date"])
            if units <= 0:
                self.add_validation(
                    check_code="fund_amount_only_order_without_units",
                    status="skipped",
                    severity="info",
                    instrument_key=fund.key,
                    source_table="fund_orders",
                    source_pk=row["fund_order_id"],
                    expected_value=amount,
                    actual_value=Decimal("0"),
                    message=f"{row['fund_order_id']} 只有金额、缺少份额，保留为原始事实但不生成 fund lot/allocation。",
                    notes=f"order_type={order_type}; cash_match_status={row['cash_match_status']}",
                )
                continue

            if order_type == "subscription":
                self.fund_lots.append(
                    FundLot(
                        fund_lot_id=self.next_id("fund_lot"),
                        account_id=self.account_id,
                        fund=fund,
                        source_type="fund_subscription",
                        source_table="fund_orders",
                        source_pk=row["fund_order_id"],
                        source_ref=row["source_refs"],
                        open_date=trade_date,
                        original_units=units,
                        remaining_units=units,
                        cost_basis_total=amount,
                        remaining_cost=amount,
                        cost_basis_status="final",
                        cost_basis_source="fund_order_amount_abs",
                        cash_source_status=row["cash_match_status"],
                        notes=f"cash_match_status={row['cash_match_status']}",
                    )
                )
            elif order_type == "redemption":
                self.fund_redemption_events.append(
                    FundRedemptionEvent(
                        order_id=row["fund_order_id"],
                        redemption_date=trade_date,
                        source_ref=row["source_refs"],
                        fund=fund,
                        units=units,
                        proceeds=amount,
                        cash_match_status=row["cash_match_status"],
                        notes=f"cash_match_status={row['cash_match_status']}",
                    )
                )
            else:
                self.add_validation(
                    check_code="fund_order_type_unhandled",
                    status="failed",
                    severity="error",
                    instrument_key=fund.key,
                    source_table="fund_orders",
                    source_pk=row["fund_order_id"],
                    message=f"{row['fund_order_id']} 基金订单类型暂无法处理：{row['fund_order_type']}",
                )

    def allocate_funds_fifo(self) -> None:
        self.fund_lots.sort(key=lambda lot: (lot.fund.key, lot.open_date or "", lot.fund_lot_id))
        self.fund_redemption_events.sort(key=lambda event: (event.redemption_date or "", event.order_id))
        lots_by_key: dict[str, list[FundLot]] = {}
        for lot in self.fund_lots:
            lots_by_key.setdefault(lot.fund.key, []).append(lot)

        for event in self.fund_redemption_events:
            remaining_units = event.units
            remaining_proceeds = event.proceeds
            candidate_lots = lots_by_key.get(event.fund.key, [])
            for lot in candidate_lots:
                if remaining_units <= 0:
                    break
                if lot.fund.currency != event.fund.currency:
                    continue
                if lot.remaining_units <= 0:
                    continue
                if event.redemption_date and lot.open_date and lot.open_date > event.redemption_date:
                    continue
                take_units = min(lot.remaining_units, remaining_units)
                is_last_piece_for_event = take_units == remaining_units
                is_closing_lot = take_units == lot.remaining_units
                if is_last_piece_for_event:
                    proceeds_allocated = q2(remaining_proceeds)
                else:
                    proceeds_allocated = q2(event.proceeds * take_units / event.units)
                if is_closing_lot:
                    cost_allocated = q2(lot.remaining_cost)
                else:
                    cost_allocated = q2(lot.unit_cost * take_units)
                realized_pnl = q2(proceeds_allocated - cost_allocated)
                self.fund_allocations.append(
                    {
                        "fund_allocation_id": self.next_id("fund_allocation"),
                        "fund_lot_id": lot.fund_lot_id,
                        "redemption_order_id": event.order_id,
                        "redemption_date": event.redemption_date,
                        "redemption_source_ref": event.source_ref,
                        "fund_key": event.fund.key,
                        "fund_code": event.fund.code,
                        "fund_name": event.fund.name,
                        "currency": event.fund.currency,
                        "units_allocated": take_units,
                        "proceeds_allocated": proceeds_allocated,
                        "cost_allocated": cost_allocated,
                        "realized_pnl": realized_pnl,
                        "cost_basis_status": lot.cost_basis_status,
                        "pnl_status": "provisional" if lot.cost_basis_status != "final" else "final",
                        "notes": event.notes,
                    }
                )
                lot.remaining_units = q6(lot.remaining_units - take_units)
                lot.remaining_cost = q2(lot.remaining_cost - cost_allocated)
                remaining_units = q6(remaining_units - take_units)
                remaining_proceeds = q2(remaining_proceeds - proceeds_allocated)

            if abs(remaining_units) <= Q6:
                self.add_validation(
                    check_code="fund_redemption_units_allocated",
                    status="passed",
                    severity="info",
                    instrument_key=event.fund.key,
                    source_table="fund_orders",
                    source_pk=event.order_id,
                    expected_value=event.units,
                    actual_value=event.units,
                    diff_value=Decimal("0"),
                    message=f"{event.order_id} 基金赎回份额已完整 FIFO 分配。",
                )
            else:
                self.add_validation(
                    check_code="fund_insufficient_lot",
                    status="failed",
                    severity="error",
                    instrument_key=event.fund.key,
                    source_table="fund_orders",
                    source_pk=event.order_id,
                    expected_value=event.units,
                    actual_value=q6(event.units - remaining_units),
                    diff_value=remaining_units,
                    message=f"{event.order_id} 可用基金 lot 不足，剩余未分配份额 {remaining_units}。",
                )

    def expected_latest_fund_units(self) -> tuple[str, dict[str, Decimal]]:
        latest_period = self.conn.execute(
            "SELECT MAX(period) FROM raw_statements WHERE import_run_id = ?",
            (self.import_run_id,),
        ).fetchone()[0]
        expected: dict[str, Decimal] = {}
        rows = self.conn.execute(
            """
            SELECT *
            FROM position_snapshots
            WHERE import_run_id = ?
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category = 'fund'
              AND quantity IS NOT NULL
            """,
            (self.import_run_id, latest_period),
        ).fetchall()
        for row in rows:
            fund = infer_fund_instrument(code_raw=row["code_name"], currency=row["currency"])
            if fund is None:
                continue
            expected[fund.key] = expected.get(fund.key, Decimal("0")) + dec(row["quantity"])
        return latest_period, expected

    def mark_fund_pending_settlement_lots(self) -> None:
        latest_period, expected = self.expected_latest_fund_units()
        latest_start = period_start_date(latest_period)
        actual: dict[str, Decimal] = {}
        for lot in self.fund_lots:
            if lot.settlement_status == "settled":
                actual[lot.fund.key] = actual.get(lot.fund.key, Decimal("0")) + lot.remaining_units

        for fund_key, actual_units in actual.items():
            excess = q6(actual_units - expected.get(fund_key, Decimal("0")))
            if excess <= Q6:
                continue
            remaining_excess = excess
            candidates = [
                lot
                for lot in self.fund_lots
                if lot.fund.key == fund_key
                and lot.source_type == "fund_subscription"
                and lot.remaining_units > 0
                and lot.open_date is not None
                and lot.open_date >= latest_start
                and lot.settlement_status == "settled"
            ]
            candidates.sort(key=lambda lot: (lot.open_date or "", lot.fund_lot_id), reverse=True)
            for lot in candidates:
                if remaining_excess <= Q6:
                    break
                if lot.remaining_units - remaining_excess > Q6:
                    self.add_validation(
                        check_code="fund_pending_settlement_partial_split_required",
                        status="failed",
                        severity="error",
                        instrument_key=fund_key,
                        source_table=lot.source_table,
                        source_pk=lot.source_pk,
                        expected_value=excess,
                        actual_value=lot.remaining_units,
                        diff_value=q6(lot.remaining_units - remaining_excess),
                        message=f"{lot.fund_lot_id} 只需部分标记待结算，当前 v1 不拆分基金 lot。",
                    )
                    break
                lot.settlement_status = "pending_settlement"
                lot.notes = f"{lot.notes or ''}; latest_statement_position_pending_settlement".strip("; ")
                remaining_excess = q6(remaining_excess - lot.remaining_units)
                self.add_validation(
                    check_code="fund_pending_settlement_lot",
                    status="passed",
                    severity="info",
                    instrument_key=fund_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    expected_value=excess,
                    actual_value=lot.remaining_units,
                    diff_value=remaining_excess,
                    message=f"{lot.fund_lot_id} 已标记为待结算基金申购，不参与 {latest_period} 期末持仓校验。",
                )
            if remaining_excess > Q6:
                self.add_validation(
                    check_code="fund_pending_settlement_unresolved",
                    status="failed",
                    severity="error",
                    instrument_key=fund_key,
                    expected_value=excess,
                    actual_value=q6(excess - remaining_excess),
                    diff_value=remaining_excess,
                    message=f"{fund_key} 多出的基金份额无法匹配为最新月份待结算申购。",
                )

    def validate_fund_remaining_positions(self) -> None:
        latest_period, expected = self.expected_latest_fund_units()
        actual: dict[str, Decimal] = {}
        for lot in self.fund_lots:
            if lot.settlement_status == "settled":
                actual[lot.fund.key] = actual.get(lot.fund.key, Decimal("0")) + lot.remaining_units

        for key in sorted(set(expected) | set(actual)):
            expected_units = q6(expected.get(key, Decimal("0")))
            actual_units = q6(actual.get(key, Decimal("0")))
            diff = q6(actual_units - expected_units)
            status = "passed" if abs(diff) <= Q6 else "failed"
            severity = "info" if status == "passed" else "error"
            self.add_validation(
                check_code="fund_ending_position_units_match",
                status=status,
                severity=severity,
                instrument_key=key,
                source_table="position_snapshots",
                source_pk=latest_period,
                expected_value=expected_units,
                actual_value=actual_units,
                diff_value=diff,
                message=f"{key} fund lot 剩余份额与 {latest_period} 期末基金持仓{'一致' if status == 'passed' else '不一致'}。",
            )

        negative_lots = [lot for lot in self.fund_lots if lot.remaining_units < 0]
        if negative_lots:
            for lot in negative_lots:
                self.add_validation(
                    check_code="fund_lot_remaining_nonnegative",
                    status="failed",
                    severity="error",
                    instrument_key=lot.fund.key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_units,
                    message=f"{lot.fund_lot_id} 出现负剩余份额。",
                )
        else:
            self.add_validation(
                check_code="fund_lot_remaining_nonnegative",
                status="passed",
                severity="info",
                message="所有基金 lot 剩余份额均非负。",
            )

    def allocate_short_stock_fifo(self) -> None:
        self.short_stock_lots.sort(key=lambda lot: (lot.instrument_key, lot.open_date or "", lot.short_lot_id))
        self.short_stock_close_events.sort(key=lambda event: (event.event_date or "", event.event_id))
        lots_by_key: dict[str, list[ShortStockLot]] = {}
        for lot in self.short_stock_lots:
            lots_by_key.setdefault(lot.instrument_key, []).append(lot)

        for event in self.short_stock_close_events:
            remaining_qty = event.quantity
            remaining_close_cash = event.closing_cash_amount
            candidate_lots = lots_by_key.get(event.instrument_key, [])
            for lot in candidate_lots:
                if remaining_qty <= 0:
                    break
                if lot.currency != event.currency:
                    continue
                if lot.remaining_quantity <= 0:
                    continue
                if event.event_date and lot.open_date and lot.open_date > event.event_date:
                    continue
                take_qty = min(lot.remaining_quantity, remaining_qty)
                is_last_piece_for_event = take_qty == remaining_qty
                is_closing_lot = take_qty == lot.remaining_quantity
                if is_last_piece_for_event:
                    closing_cash_allocated = q2(remaining_close_cash)
                else:
                    closing_cash_allocated = q2(event.closing_cash_amount * take_qty / event.quantity)
                if is_closing_lot:
                    opening_cash_allocated = q2(lot.remaining_opening_cash_amount)
                else:
                    opening_cash_allocated = q2(lot.remaining_opening_cash_amount * take_qty / lot.remaining_quantity)
                realized_pnl = q2(opening_cash_allocated + closing_cash_allocated)
                self.short_stock_allocations.append(
                    {
                        "short_allocation_id": self.next_id("short_allocation"),
                        "short_lot_id": lot.short_lot_id,
                        "close_event_table": event.event_table,
                        "close_event_id": event.event_id,
                        "close_event_date": event.event_date,
                        "close_settlement_date": event.settlement_date,
                        "close_source_ref": event.source_ref,
                        "instrument_key": event.instrument_key,
                        "instrument_code": event.instrument_code,
                        "instrument_name": event.instrument_name,
                        "currency": event.currency,
                        "quantity_allocated": take_qty,
                        "opening_cash_allocated": opening_cash_allocated,
                        "closing_cash_allocated": closing_cash_allocated,
                        "realized_pnl": realized_pnl,
                        "pnl_status": "final",
                        "notes": event.notes,
                    }
                )
                lot.remaining_quantity = q6(lot.remaining_quantity - take_qty)
                lot.remaining_opening_cash_amount = q2(lot.remaining_opening_cash_amount - opening_cash_allocated)
                remaining_qty = q6(remaining_qty - take_qty)
                remaining_close_cash = q2(remaining_close_cash - closing_cash_allocated)

            if abs(remaining_qty) <= Q6:
                self.add_validation(
                    check_code="short_stock_close_quantity_allocated",
                    status="passed",
                    severity="info",
                    instrument_key=event.instrument_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.quantity,
                    actual_value=event.quantity,
                    diff_value=Decimal("0"),
                    message=f"{event.event_id} 股票短仓买回数量已完整 FIFO 分配。",
                )
            else:
                self.add_validation(
                    check_code="short_stock_insufficient_lot",
                    status="failed",
                    severity="error",
                    instrument_key=event.instrument_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.quantity,
                    actual_value=q6(event.quantity - remaining_qty),
                    diff_value=remaining_qty,
                    message=f"{event.event_id} 可用短仓 lot 不足，剩余未分配数量 {remaining_qty}。",
                )

    def validate_short_stock_remaining_positions(self) -> None:
        negative_lots = [lot for lot in self.short_stock_lots if lot.remaining_quantity < 0]
        if negative_lots:
            for lot in negative_lots:
                self.add_validation(
                    check_code="short_stock_lot_remaining_nonnegative",
                    status="failed",
                    severity="error",
                    instrument_key=lot.instrument_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_quantity,
                    message=f"{lot.short_lot_id} 出现负剩余数量。",
                )
        else:
            self.add_validation(
                check_code="short_stock_lot_remaining_nonnegative",
                status="passed",
                severity="info",
                message="所有股票短仓 lot 剩余数量均非负。",
            )

        open_lots = [lot for lot in self.short_stock_lots if abs(lot.remaining_quantity) > Q6]
        if not open_lots:
            self.add_validation(
                check_code="short_stock_open_positions_closed",
                status="passed",
                severity="info",
                message="本次样本内股票短仓均已买回平仓。",
            )
        else:
            for lot in open_lots:
                self.add_validation(
                    check_code="short_stock_open_position_carried",
                    status="skipped",
                    severity="warning",
                    instrument_key=lot.instrument_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_quantity,
                    message=f"{lot.short_lot_id} 仍有股票短仓未平仓；需要后续借券/保证金口径样本继续校验。",
                )

    def allocate_fifo(self) -> None:
        self.lots.sort(key=lambda lot: (lot.instrument_key, lot.open_date or "", lot.lot_id))
        self.close_events.sort(key=lambda event: (event.event_date or "", event.event_id))
        lots_by_key: dict[str, list[Lot]] = {}
        for lot in self.lots:
            lots_by_key.setdefault(lot.instrument_key, []).append(lot)

        for event in self.close_events:
            remaining_qty = event.quantity
            remaining_proceeds = event.proceeds
            candidate_lots = lots_by_key.get(event.instrument_key, [])
            for lot in candidate_lots:
                if remaining_qty <= 0:
                    break
                if lot.currency != event.currency:
                    continue
                if lot.remaining_quantity <= 0:
                    continue
                if event.event_date and lot.open_date and lot.open_date > event.event_date:
                    continue
                take_qty = min(lot.remaining_quantity, remaining_qty)
                is_last_piece_for_event = take_qty == remaining_qty
                is_closing_lot = take_qty == lot.remaining_quantity
                if is_last_piece_for_event:
                    proceeds_allocated = q2(remaining_proceeds)
                else:
                    proceeds_allocated = q2(event.proceeds * take_qty / event.quantity)
                if is_closing_lot:
                    cost_allocated = q2(lot.remaining_cost)
                else:
                    cost_allocated = q2(lot.unit_cost * take_qty)
                realized_pnl = q2(proceeds_allocated - cost_allocated)
                allocation_id = self.next_id("allocation")
                self.allocations.append(
                    {
                        "allocation_id": allocation_id,
                        "close_event_table": event.event_table,
                        "close_event_id": event.event_id,
                        "close_event_date": event.event_date,
                        "close_settlement_date": event.settlement_date,
                        "close_source_ref": event.source_ref,
                        "instrument_key": event.instrument_key,
                        "instrument_code": event.instrument_code,
                        "instrument_name": event.instrument_name,
                        "currency": event.currency,
                        "lot_id": lot.lot_id,
                        "quantity_allocated": take_qty,
                        "proceeds_allocated": proceeds_allocated,
                        "cost_allocated": cost_allocated,
                        "realized_pnl": realized_pnl,
                        "cost_basis_status": lot.cost_basis_status,
                        "pnl_status": "provisional" if lot.cost_basis_status != "final" else "final",
                        "notes": event.notes,
                    }
                )
                lot.remaining_quantity = q6(lot.remaining_quantity - take_qty)
                lot.remaining_cost = q2(lot.remaining_cost - cost_allocated)
                remaining_qty = q6(remaining_qty - take_qty)
                remaining_proceeds = q2(remaining_proceeds - proceeds_allocated)

            if abs(remaining_qty) <= Q6:
                self.add_validation(
                    check_code="close_quantity_allocated",
                    status="passed",
                    severity="info",
                    instrument_key=event.instrument_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.quantity,
                    actual_value=event.quantity,
                    diff_value=Decimal("0"),
                    message=f"{event.event_id} 卖出数量已完整 FIFO 分配。",
                )
            else:
                self.add_validation(
                    check_code="insufficient_lot",
                    status="failed",
                    severity="error",
                    instrument_key=event.instrument_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.quantity,
                    actual_value=q6(event.quantity - remaining_qty),
                    diff_value=remaining_qty,
                    message=f"{event.event_id} 可用 lot 不足，剩余未分配数量 {remaining_qty}。",
                )

    def validate_remaining_positions(self) -> None:
        latest_period = self.conn.execute(
            "SELECT MAX(period) FROM raw_statements WHERE import_run_id = ?",
            (self.import_run_id,),
        ).fetchone()[0]
        expected: dict[str, Decimal] = {}
        expected_names: dict[str, tuple[str | None, str | None, str | None]] = {}
        rows = self.conn.execute(
            """
            SELECT *
            FROM position_snapshots
            WHERE import_run_id = ?
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category = 'stock_or_option'
              AND quantity IS NOT NULL
            """,
            (self.import_run_id, latest_period),
        ).fetchall()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if not inst.eligible_long_equity or not inst.key:
                continue
            expected[inst.key] = expected.get(inst.key, Decimal("0")) + dec(row["quantity"])
            expected_names[inst.key] = (inst.code, inst.name, text(row["currency"]).upper())

        actual: dict[str, Decimal] = {}
        for lot in self.lots:
            actual[lot.instrument_key] = actual.get(lot.instrument_key, Decimal("0")) + lot.remaining_quantity
            expected_names.setdefault(lot.instrument_key, (lot.instrument_code, lot.instrument_name, lot.currency))

        for key in sorted(set(expected) | set(actual)):
            expected_qty = q6(expected.get(key, Decimal("0")))
            actual_qty = q6(actual.get(key, Decimal("0")))
            diff = q6(actual_qty - expected_qty)
            status = "passed" if abs(diff) <= Q6 else "failed"
            severity = "info" if status == "passed" else "error"
            self.add_validation(
                check_code="ending_position_quantity_match",
                status=status,
                severity=severity,
                instrument_key=key,
                source_table="position_snapshots",
                source_pk=latest_period,
                expected_value=expected_qty,
                actual_value=actual_qty,
                diff_value=diff,
                message=f"{key} lot 剩余数量与 {latest_period} 期末持仓{'一致' if status == 'passed' else '不一致'}。",
            )

        negative_lots = [lot for lot in self.lots if lot.remaining_quantity < 0]
        if negative_lots:
            for lot in negative_lots:
                self.add_validation(
                    check_code="lot_remaining_nonnegative",
                    status="failed",
                    severity="error",
                    instrument_key=lot.instrument_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_quantity,
                    message=f"{lot.lot_id} 出现负剩余数量。",
                )
        else:
            self.add_validation(
                check_code="lot_remaining_nonnegative",
                status="passed",
                severity="info",
                message="所有 lot 剩余数量均非负。",
            )

    def persist(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO position_lots (
              allocation_run_id, lot_id, account_id, instrument_key, instrument_code, instrument_name,
              market, currency, source_type, source_table, source_pk, source_ref, open_date, settlement_date,
              original_quantity, remaining_quantity, cost_basis_total, cost_basis_principal, cost_basis_fee,
              cost_basis_currency, cost_basis_status, cost_basis_source, unit_cost, lot_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    lot.lot_id,
                    lot.account_id,
                    lot.instrument_key,
                    lot.instrument_code,
                    lot.instrument_name,
                    lot.market,
                    lot.currency,
                    lot.source_type,
                    lot.source_table,
                    lot.source_pk,
                    lot.source_ref,
                    lot.open_date,
                    lot.settlement_date,
                    dbnum(lot.original_quantity),
                    dbnum(lot.remaining_quantity),
                    dbnum(q2(lot.cost_basis_total)),
                    dbnum(q2(lot.cost_basis_principal)),
                    dbnum(q2(lot.cost_basis_fee)),
                    lot.currency,
                    lot.cost_basis_status,
                    lot.cost_basis_source,
                    dbnum(lot.unit_cost),
                    lot.lot_status,
                    lot.notes,
                )
                for lot in self.lots
            ],
        )
        component_rows: list[tuple[Any, ...]] = []
        for lot in self.lots:
            for component in lot.components:
                component_rows.append(
                    (
                        self.run_id,
                        component["component_id"],
                        component["lot_id"],
                        component["component_type"],
                        dbnum(q2(component["amount"])),
                        component["currency"],
                        component["source_table"],
                        component["source_pk"],
                        component["source_ref"],
                        "capitalized_to_lot",
                        component["formula"],
                        component["notes"],
                    )
                )
        self.conn.executemany(
            """
            INSERT INTO lot_cost_components (
              allocation_run_id, component_id, lot_id, component_type, amount, currency,
              source_table, source_pk, source_ref, cost_treatment, formula, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            component_rows,
        )
        self.conn.executemany(
            """
            INSERT INTO lot_allocations (
              allocation_run_id, allocation_id, close_event_table, close_event_id,
              close_event_date, close_settlement_date, close_source_ref,
              instrument_key, instrument_code, instrument_name, currency, lot_id,
              allocation_method, quantity_allocated, proceeds_allocated, cost_allocated,
              realized_pnl, cost_basis_status, pnl_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fifo', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    allocation["allocation_id"],
                    allocation["close_event_table"],
                    allocation["close_event_id"],
                    allocation["close_event_date"],
                    allocation["close_settlement_date"],
                    allocation["close_source_ref"],
                    allocation["instrument_key"],
                    allocation["instrument_code"],
                    allocation["instrument_name"],
                    allocation["currency"],
                    allocation["lot_id"],
                    dbnum(allocation["quantity_allocated"]),
                    dbnum(q2(allocation["proceeds_allocated"])),
                    dbnum(q2(allocation["cost_allocated"])),
                    dbnum(q2(allocation["realized_pnl"])),
                    allocation["cost_basis_status"],
                    allocation["pnl_status"],
                    allocation["notes"],
                )
                for allocation in self.allocations
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO option_contract_lots (
              allocation_run_id, option_lot_id, account_id, option_contract_key, option_code,
              underlying_symbol, underlying_instrument_key, expiry_date, strike_price, option_type,
              contract_multiplier, market, currency, position_side, source_type, source_table,
              source_pk, source_ref, open_date, settlement_date, original_contracts,
              remaining_contracts, opening_net_cash_amount, remaining_opening_cash_amount,
              opening_gross_amount, opening_fee_total, premium_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    lot.option_lot_id,
                    lot.account_id,
                    lot.contract.contract_key,
                    lot.contract.code,
                    lot.contract.underlying_symbol,
                    lot.contract.underlying_instrument_key,
                    lot.contract.expiry_date,
                    dbnum(lot.contract.strike_price),
                    lot.contract.option_type,
                    dbnum(lot.contract_multiplier) if lot.contract_multiplier is not None else None,
                    lot.contract.market,
                    lot.contract.currency,
                    lot.position_side,
                    lot.source_type,
                    lot.source_table,
                    lot.source_pk,
                    lot.source_ref,
                    lot.open_date,
                    lot.settlement_date,
                    dbnum(lot.original_contracts),
                    dbnum(lot.remaining_contracts),
                    dbnum(q2(lot.opening_net_cash_amount)),
                    dbnum(q2(lot.remaining_opening_cash_amount)),
                    dbnum(q2(lot.opening_gross_amount)),
                    dbnum(q2(lot.opening_fee_total)),
                    lot.premium_status,
                    lot.notes,
                )
                for lot in self.option_lots
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO option_lot_allocations (
              allocation_run_id, option_allocation_id, option_lot_id, option_contract_key,
              option_code, underlying_symbol, underlying_instrument_key, expiry_date,
              strike_price, option_type, currency, position_side, close_event_type,
              close_outcome, close_event_table, close_event_id, close_event_date,
              close_settlement_date, close_source_ref, allocation_method, contracts_allocated,
              opening_cash_allocated, closing_cash_allocated, realized_pnl, pnl_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fifo', ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    allocation["option_allocation_id"],
                    allocation["option_lot_id"],
                    allocation["option_contract_key"],
                    allocation["option_code"],
                    allocation["underlying_symbol"],
                    allocation["underlying_instrument_key"],
                    allocation["expiry_date"],
                    dbnum(allocation["strike_price"]),
                    allocation["option_type"],
                    allocation["currency"],
                    allocation["position_side"],
                    allocation["close_event_type"],
                    allocation["close_outcome"],
                    allocation["close_event_table"],
                    allocation["close_event_id"],
                    allocation["close_event_date"],
                    allocation["close_settlement_date"],
                    allocation["close_source_ref"],
                    dbnum(allocation["contracts_allocated"]),
                    dbnum(q2(allocation["opening_cash_allocated"])),
                    dbnum(q2(allocation["closing_cash_allocated"])),
                    dbnum(q2(allocation["realized_pnl"])),
                    allocation["pnl_status"],
                    allocation["notes"],
                )
                for allocation in self.option_allocations
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO option_underlying_links (
              allocation_run_id, link_id, option_allocation_id, option_lot_id,
              option_contract_key, link_type, underlying_event_table, underlying_event_id,
              underlying_instrument_key, underlying_quantity, strike_price,
              underlying_gross_amount, confidence, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    link["link_id"],
                    link["option_allocation_id"],
                    link["option_lot_id"],
                    link["option_contract_key"],
                    link["link_type"],
                    link["underlying_event_table"],
                    link["underlying_event_id"],
                    link["underlying_instrument_key"],
                    dbnum(link["underlying_quantity"]),
                    dbnum(link["strike_price"]),
                    dbnum(link["underlying_gross_amount"]),
                    link["confidence"],
                    link["notes"],
                )
                for link in self.option_underlying_links
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO fund_position_lots (
              allocation_run_id, fund_lot_id, account_id, fund_key, fund_code, fund_name,
              currency, source_type, source_table, source_pk, source_ref, open_date,
              original_units, remaining_units, cost_basis_total, remaining_cost,
              cost_basis_currency, cost_basis_status, cost_basis_source, unit_cost,
              cash_source_status, settlement_status, lot_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    lot.fund_lot_id,
                    lot.account_id,
                    lot.fund.key,
                    lot.fund.code,
                    lot.fund.name,
                    lot.fund.currency,
                    lot.source_type,
                    lot.source_table,
                    lot.source_pk,
                    lot.source_ref,
                    lot.open_date,
                    dbnum(lot.original_units),
                    dbnum(lot.remaining_units),
                    dbnum(q2(lot.cost_basis_total)),
                    dbnum(q2(lot.remaining_cost)),
                    lot.fund.currency,
                    lot.cost_basis_status,
                    lot.cost_basis_source,
                    dbnum(lot.unit_cost),
                    lot.cash_source_status,
                    lot.settlement_status,
                    lot.lot_status,
                    lot.notes,
                )
                for lot in self.fund_lots
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO fund_lot_allocations (
              allocation_run_id, fund_allocation_id, fund_lot_id, redemption_order_id,
              redemption_date, redemption_source_ref, fund_key, fund_code, fund_name,
              currency, allocation_method, units_allocated, proceeds_allocated,
              cost_allocated, realized_pnl, cost_basis_status, pnl_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fifo', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    allocation["fund_allocation_id"],
                    allocation["fund_lot_id"],
                    allocation["redemption_order_id"],
                    allocation["redemption_date"],
                    allocation["redemption_source_ref"],
                    allocation["fund_key"],
                    allocation["fund_code"],
                    allocation["fund_name"],
                    allocation["currency"],
                    dbnum(allocation["units_allocated"]),
                    dbnum(q2(allocation["proceeds_allocated"])),
                    dbnum(q2(allocation["cost_allocated"])),
                    dbnum(q2(allocation["realized_pnl"])),
                    allocation["cost_basis_status"],
                    allocation["pnl_status"],
                    allocation["notes"],
                )
                for allocation in self.fund_allocations
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO short_stock_lots (
              allocation_run_id, short_lot_id, account_id, instrument_key, instrument_code,
              instrument_name, market, currency, source_table, source_pk, source_ref,
              open_date, settlement_date, original_quantity, remaining_quantity,
              opening_net_cash_amount, remaining_opening_cash_amount, opening_gross_amount,
              opening_fee_total, lot_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    lot.short_lot_id,
                    lot.account_id,
                    lot.instrument_key,
                    lot.instrument_code,
                    lot.instrument_name,
                    lot.market,
                    lot.currency,
                    lot.source_table,
                    lot.source_pk,
                    lot.source_ref,
                    lot.open_date,
                    lot.settlement_date,
                    dbnum(lot.original_quantity),
                    dbnum(lot.remaining_quantity),
                    dbnum(q2(lot.opening_net_cash_amount)),
                    dbnum(q2(lot.remaining_opening_cash_amount)),
                    dbnum(q2(lot.opening_gross_amount)),
                    dbnum(q2(lot.opening_fee_total)),
                    lot.lot_status,
                    lot.notes,
                )
                for lot in self.short_stock_lots
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO short_stock_allocations (
              allocation_run_id, short_allocation_id, short_lot_id, close_event_table,
              close_event_id, close_event_date, close_settlement_date, close_source_ref,
              instrument_key, instrument_code, instrument_name, currency, allocation_method,
              quantity_allocated, opening_cash_allocated, closing_cash_allocated,
              realized_pnl, pnl_status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fifo', ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    allocation["short_allocation_id"],
                    allocation["short_lot_id"],
                    allocation["close_event_table"],
                    allocation["close_event_id"],
                    allocation["close_event_date"],
                    allocation["close_settlement_date"],
                    allocation["close_source_ref"],
                    allocation["instrument_key"],
                    allocation["instrument_code"],
                    allocation["instrument_name"],
                    allocation["currency"],
                    dbnum(allocation["quantity_allocated"]),
                    dbnum(q2(allocation["opening_cash_allocated"])),
                    dbnum(q2(allocation["closing_cash_allocated"])),
                    dbnum(q2(allocation["realized_pnl"])),
                    allocation["pnl_status"],
                    allocation["notes"],
                )
                for allocation in self.short_stock_allocations
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO lot_allocation_validation_items (
              allocation_run_id, validation_item_id, check_code, status, severity,
              instrument_key, source_table, source_pk, expected_value, actual_value,
              diff_value, message, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.run_id,
                    item["validation_item_id"],
                    item["check_code"],
                    item["status"],
                    item["severity"],
                    item["instrument_key"],
                    item["source_table"],
                    item["source_pk"],
                    dbnum(item["expected_value"]) if item["expected_value"] is not None else None,
                    dbnum(item["actual_value"]) if item["actual_value"] is not None else None,
                    dbnum(item["diff_value"]) if item["diff_value"] is not None else None,
                    item["message"],
                    item["notes"],
                )
                for item in self.validation_items
            ],
        )


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


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def latest_import_run_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT import_run_id FROM import_runs ORDER BY created_at DESC LIMIT 1",
    ).fetchone()
    if row is None:
        raise RuntimeError("No import_runs found in database.")
    return row[0]


def delete_existing_run(conn: sqlite3.Connection, allocation_run_id: str) -> None:
    for table in (
        "lot_allocation_validation_items",
        "short_stock_allocations",
        "short_stock_lots",
        "fund_lot_allocations",
        "fund_position_lots",
        "option_underlying_links",
        "option_lot_allocations",
        "option_contract_lots",
        "lot_allocations",
        "lot_cost_components",
        "position_lots",
        "lot_allocation_runs",
    ):
        if table_exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE allocation_run_id = ?", (allocation_run_id,))


def run_allocation(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    schema_path = args.schema_path.resolve()
    report_path = args.report_path.resolve()
    export_dir = args.export_dir.resolve()
    allocation_run_id = args.run_id or f"lot_allocation_v1_{utc_now_compact()}"

    with connect(db_path) as conn:
        apply_schema(conn, schema_path)
        import_run_id = args.import_run_id or latest_import_run_id(conn)
        if args.replace:
            delete_existing_run(conn, allocation_run_id)
        conn.execute(
            """
            INSERT INTO lot_allocation_runs (
              allocation_run_id, import_run_id, account_id, method, scope, status, notes
            )
            VALUES (?, ?, ?, 'fifo', 'stock_ipo_option_fund_short_v1', 'running', ?)
            """,
            (allocation_run_id, import_run_id, args.account_id, "正股 / ETF long position + IPO 中签配发 + 期权合约 + 基金申赎 + 股票短仓 FIFO allocation v1。"),
        )
        allocator = LotAllocator(conn, allocation_run_id, import_run_id, args.account_id)
        allocator.load_opening_lots()
        allocator.load_ipo_allotment_lots()
        allocator.load_fund_lots_and_redemptions()
        allocator.load_market_trade_lots_and_closes()
        allocator.load_option_lots_and_closes()
        allocator.load_option_exercise_events()
        allocator.allocate_fifo()
        allocator.allocate_options_fifo()
        allocator.allocate_funds_fifo()
        allocator.allocate_short_stock_fifo()
        allocator.mark_fund_pending_settlement_lots()
        allocator.validate_remaining_positions()
        allocator.validate_option_remaining_positions()
        allocator.validate_fund_remaining_positions()
        allocator.validate_short_stock_remaining_positions()
        allocator.persist()

        failed_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM lot_allocation_validation_items
            WHERE allocation_run_id = ?
              AND status = 'failed'
            """,
            (allocation_run_id,),
        ).fetchone()[0]
        warning_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM lot_allocation_validation_items
            WHERE allocation_run_id = ?
              AND severity = 'warning'
            """,
            (allocation_run_id,),
        ).fetchone()[0]
        status = "failed" if failed_count else ("passed_with_warnings" if warning_count else "passed")
        conn.execute(
            "UPDATE lot_allocation_runs SET status = ? WHERE allocation_run_id = ?",
            (status, allocation_run_id),
        )
        conn.commit()

        summary = build_summary(conn, allocation_run_id)
        write_exports(conn, allocation_run_id, export_dir)
        write_report(report_path, db_path, allocation_run_id, summary)
    return {
        "status": summary["run"]["status"],
        "allocation_run_id": allocation_run_id,
        "db_path": str(db_path),
        "report_path": str(report_path),
        "export_dir": str(export_dir),
        "summary": summary,
    }


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def build_summary(conn: sqlite3.Connection, allocation_run_id: str) -> dict[str, Any]:
    run = dict(
        conn.execute(
            "SELECT * FROM lot_allocation_runs WHERE allocation_run_id = ?",
            (allocation_run_id,),
        ).fetchone()
    )
    lot_counts = rows_as_dicts(
        conn,
        """
        SELECT source_type, cost_basis_status, currency, COUNT(*) AS lot_count,
               ROUND(SUM(original_quantity), 6) AS original_quantity,
               ROUND(SUM(remaining_quantity), 6) AS remaining_quantity,
               ROUND(SUM(cost_basis_total), 2) AS cost_basis_total
        FROM position_lots
        WHERE allocation_run_id = ?
        GROUP BY source_type, cost_basis_status, currency
        ORDER BY source_type, currency
        """,
        (allocation_run_id,),
    )
    pnl_by_currency_status = rows_as_dicts(
        conn,
        """
        SELECT currency, pnl_status, COUNT(*) AS allocation_count,
               ROUND(SUM(quantity_allocated), 6) AS quantity_allocated,
               ROUND(SUM(proceeds_allocated), 2) AS proceeds_total,
               ROUND(SUM(cost_allocated), 2) AS cost_total,
               ROUND(SUM(realized_pnl), 2) AS realized_pnl
        FROM lot_allocations
        WHERE allocation_run_id = ?
        GROUP BY currency, pnl_status
        ORDER BY currency, pnl_status
        """,
        (allocation_run_id,),
    )
    pnl_by_instrument = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_lot_realized_pnl_by_instrument
        WHERE allocation_run_id = ?
        ORDER BY currency, realized_pnl DESC
        """,
        (allocation_run_id,),
    )
    option_lot_counts = rows_as_dicts(
        conn,
        """
        SELECT source_type, position_side, currency, COUNT(*) AS lot_count,
               ROUND(SUM(original_contracts), 6) AS original_contracts,
               ROUND(SUM(remaining_contracts), 6) AS remaining_contracts,
               ROUND(SUM(opening_net_cash_amount), 2) AS opening_net_cash_amount,
               ROUND(SUM(remaining_opening_cash_amount), 2) AS remaining_opening_cash_amount
        FROM option_contract_lots
        WHERE allocation_run_id = ?
        GROUP BY source_type, position_side, currency
        ORDER BY currency, source_type, position_side
        """,
        (allocation_run_id,),
    )
    option_pnl_by_currency = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_option_realized_pnl_by_currency
        WHERE allocation_run_id = ?
        ORDER BY currency, pnl_status
        """,
        (allocation_run_id,),
    )
    option_pnl_by_contract = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_option_realized_pnl_by_contract
        WHERE allocation_run_id = ?
        ORDER BY currency, realized_pnl DESC
        """,
        (allocation_run_id,),
    )
    option_open_positions = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_option_open_positions
        WHERE allocation_run_id = ?
        ORDER BY currency, option_contract_key
        """,
        (allocation_run_id,),
    )
    option_underlying_links = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM option_underlying_links
        WHERE allocation_run_id = ?
        ORDER BY option_contract_key, link_id
        """,
        (allocation_run_id,),
    )
    fund_lot_counts = rows_as_dicts(
        conn,
        """
        SELECT source_type, cost_basis_status, settlement_status, currency, COUNT(*) AS lot_count,
               ROUND(SUM(original_units), 6) AS original_units,
               ROUND(SUM(remaining_units), 6) AS remaining_units,
               ROUND(SUM(cost_basis_total), 2) AS cost_basis_total,
               ROUND(SUM(remaining_cost), 2) AS remaining_cost
        FROM fund_position_lots
        WHERE allocation_run_id = ?
        GROUP BY source_type, cost_basis_status, settlement_status, currency
        ORDER BY currency, source_type, settlement_status
        """,
        (allocation_run_id,),
    )
    fund_pnl_by_currency = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_fund_realized_pnl_by_currency
        WHERE allocation_run_id = ?
        ORDER BY currency, pnl_status
        """,
        (allocation_run_id,),
    )
    fund_pnl_by_fund = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_fund_realized_pnl_by_fund
        WHERE allocation_run_id = ?
        ORDER BY currency, realized_pnl DESC
        """,
        (allocation_run_id,),
    )
    fund_open_positions = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_fund_open_positions
        WHERE allocation_run_id = ?
        ORDER BY currency, fund_key
        """,
        (allocation_run_id,),
    )
    fund_pending_positions = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_fund_pending_positions
        WHERE allocation_run_id = ?
        ORDER BY currency, fund_key
        """,
        (allocation_run_id,),
    )
    short_lot_counts = rows_as_dicts(
        conn,
        """
        SELECT currency, COUNT(*) AS lot_count,
               ROUND(SUM(original_quantity), 6) AS original_quantity,
               ROUND(SUM(remaining_quantity), 6) AS remaining_quantity,
               ROUND(SUM(opening_net_cash_amount), 2) AS opening_net_cash_amount,
               ROUND(SUM(remaining_opening_cash_amount), 2) AS remaining_opening_cash_amount
        FROM short_stock_lots
        WHERE allocation_run_id = ?
        GROUP BY currency
        ORDER BY currency
        """,
        (allocation_run_id,),
    )
    short_pnl_by_currency = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_short_stock_realized_pnl_by_currency
        WHERE allocation_run_id = ?
        ORDER BY currency, pnl_status
        """,
        (allocation_run_id,),
    )
    short_pnl_by_instrument = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_short_stock_realized_pnl_by_instrument
        WHERE allocation_run_id = ?
        ORDER BY currency, realized_pnl DESC
        """,
        (allocation_run_id,),
    )
    short_open_positions = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM v_short_stock_open_positions
        WHERE allocation_run_id = ?
        ORDER BY currency, instrument_key
        """,
        (allocation_run_id,),
    )
    total_pnl_by_currency_status = rows_as_dicts(
        conn,
        """
        SELECT pnl_layer, currency, pnl_status, realized_pnl
        FROM v_total_realized_pnl_by_currency_status
        WHERE allocation_run_id = ?
        ORDER BY currency, pnl_layer, pnl_status
        """,
        (allocation_run_id,),
    )
    validation_counts = rows_as_dicts(
        conn,
        """
        SELECT check_code, status, severity, COUNT(*) AS item_count
        FROM lot_allocation_validation_items
        WHERE allocation_run_id = ?
        GROUP BY check_code, status, severity
        ORDER BY severity DESC, check_code, status
        """,
        (allocation_run_id,),
    )
    failed_items = rows_as_dicts(
        conn,
        """
        SELECT *
        FROM lot_allocation_validation_items
        WHERE allocation_run_id = ?
          AND status = 'failed'
        ORDER BY check_code, validation_item_id
        """,
        (allocation_run_id,),
    )
    deferred_items = rows_as_dicts(
        conn,
        """
        SELECT check_code, severity, COUNT(*) AS item_count
        FROM lot_allocation_validation_items
        WHERE allocation_run_id = ?
          AND status = 'skipped'
        GROUP BY check_code, severity
        ORDER BY check_code, severity
        """,
        (allocation_run_id,),
    )
    return {
        "run": run,
        "lot_counts": lot_counts,
        "pnl_by_currency_status": pnl_by_currency_status,
        "pnl_by_instrument": pnl_by_instrument,
        "option_lot_counts": option_lot_counts,
        "option_pnl_by_currency": option_pnl_by_currency,
        "option_pnl_by_contract": option_pnl_by_contract,
        "option_open_positions": option_open_positions,
        "option_underlying_links": option_underlying_links,
        "fund_lot_counts": fund_lot_counts,
        "fund_pnl_by_currency": fund_pnl_by_currency,
        "fund_pnl_by_fund": fund_pnl_by_fund,
        "fund_open_positions": fund_open_positions,
        "fund_pending_positions": fund_pending_positions,
        "short_lot_counts": short_lot_counts,
        "short_pnl_by_currency": short_pnl_by_currency,
        "short_pnl_by_instrument": short_pnl_by_instrument,
        "short_open_positions": short_open_positions,
        "total_pnl_by_currency_status": total_pnl_by_currency_status,
        "validation_counts": validation_counts,
        "failed_items": failed_items,
        "deferred_items": deferred_items,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_exports(conn: sqlite3.Connection, allocation_run_id: str, export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        export_dir / "position_lots.csv",
        rows_as_dicts(conn, "SELECT * FROM position_lots WHERE allocation_run_id = ? ORDER BY instrument_key, open_date, lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "lot_cost_components.csv",
        rows_as_dicts(conn, "SELECT * FROM lot_cost_components WHERE allocation_run_id = ? ORDER BY lot_id, component_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "lot_allocations.csv",
        rows_as_dicts(conn, "SELECT * FROM v_lot_allocations_enriched WHERE allocation_run_id = ? ORDER BY close_event_date, close_event_id, lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "realized_pnl_by_instrument.csv",
        rows_as_dicts(conn, "SELECT * FROM v_lot_realized_pnl_by_instrument WHERE allocation_run_id = ? ORDER BY currency, realized_pnl DESC", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "open_positions.csv",
        rows_as_dicts(conn, "SELECT * FROM v_lot_open_positions WHERE allocation_run_id = ? ORDER BY currency, instrument_key", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "option_contract_lots.csv",
        rows_as_dicts(conn, "SELECT * FROM option_contract_lots WHERE allocation_run_id = ? ORDER BY currency, option_contract_key, open_date, option_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "option_lot_allocations.csv",
        rows_as_dicts(conn, "SELECT * FROM option_lot_allocations WHERE allocation_run_id = ? ORDER BY close_event_date, close_event_id, option_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "option_realized_pnl_by_contract.csv",
        rows_as_dicts(conn, "SELECT * FROM v_option_realized_pnl_by_contract WHERE allocation_run_id = ? ORDER BY currency, realized_pnl DESC", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "option_open_positions.csv",
        rows_as_dicts(conn, "SELECT * FROM v_option_open_positions WHERE allocation_run_id = ? ORDER BY currency, option_contract_key", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "option_underlying_links.csv",
        rows_as_dicts(conn, "SELECT * FROM option_underlying_links WHERE allocation_run_id = ? ORDER BY option_contract_key, link_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "fund_position_lots.csv",
        rows_as_dicts(conn, "SELECT * FROM fund_position_lots WHERE allocation_run_id = ? ORDER BY currency, fund_key, open_date, fund_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "fund_lot_allocations.csv",
        rows_as_dicts(conn, "SELECT * FROM fund_lot_allocations WHERE allocation_run_id = ? ORDER BY redemption_date, redemption_order_id, fund_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "fund_realized_pnl_by_fund.csv",
        rows_as_dicts(conn, "SELECT * FROM v_fund_realized_pnl_by_fund WHERE allocation_run_id = ? ORDER BY currency, realized_pnl DESC", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "fund_open_positions.csv",
        rows_as_dicts(conn, "SELECT * FROM v_fund_open_positions WHERE allocation_run_id = ? ORDER BY currency, fund_key", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "fund_pending_positions.csv",
        rows_as_dicts(conn, "SELECT * FROM v_fund_pending_positions WHERE allocation_run_id = ? ORDER BY currency, fund_key", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "short_stock_lots.csv",
        rows_as_dicts(conn, "SELECT * FROM short_stock_lots WHERE allocation_run_id = ? ORDER BY currency, instrument_key, open_date, short_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "short_stock_allocations.csv",
        rows_as_dicts(conn, "SELECT * FROM short_stock_allocations WHERE allocation_run_id = ? ORDER BY close_event_date, close_event_id, short_lot_id", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "short_stock_realized_pnl_by_instrument.csv",
        rows_as_dicts(conn, "SELECT * FROM v_short_stock_realized_pnl_by_instrument WHERE allocation_run_id = ? ORDER BY currency, realized_pnl DESC", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "short_stock_open_positions.csv",
        rows_as_dicts(conn, "SELECT * FROM v_short_stock_open_positions WHERE allocation_run_id = ? ORDER BY currency, instrument_key", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "total_realized_pnl_by_currency_status.csv",
        rows_as_dicts(conn, "SELECT * FROM v_total_realized_pnl_by_currency_status WHERE allocation_run_id = ? ORDER BY currency, pnl_layer, pnl_status", (allocation_run_id,)),
    )
    write_csv(
        export_dir / "validation_items.csv",
        rows_as_dicts(conn, "SELECT * FROM lot_allocation_validation_items WHERE allocation_run_id = ? ORDER BY status, severity, check_code, validation_item_id", (allocation_run_id,)),
    )


def md_table(rows: list[dict[str, Any]], columns: list[str], max_rows: int | None = None) -> str:
    display_rows = rows[:max_rows] if max_rows is not None else rows
    if not display_rows:
        return "_无_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in display_rows:
        lines.append("| " + " | ".join(str(row.get(col, "") if row.get(col, "") is not None else "") for col in columns) + " |")
    if max_rows is not None and len(rows) > max_rows:
        lines.append(f"\n_仅展示前 {max_rows} 行，共 {len(rows)} 行。_")
    return "\n".join(lines)


def write_report(report_path: Path, db_path: Path, allocation_run_id: str, summary: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run = summary["run"]
    lines = [
        "# Lot / Allocation v1 运行报告",
        "",
        f"- 运行 ID：`{allocation_run_id}`",
        f"- 数据库：`{db_path}`",
        f"- 状态：`{run['status']}`",
        f"- 生成时间：`{utc_now_iso()}`",
        f"- 范围：正股 / ETF long position、IPO 中签配发、二级市场买入/卖出平仓、期权开仓/平仓/到期/指派、基金申赎、股票短仓；FIFO。",
        f"- 期初 lot 成本：使用首份结单期初市值，标记为 `provisional`。",
        f"- IPO lot 成本：配发金额 + 显式申购费 + `配发金额 * 1.0085%`；融资利息不分摊。",
        f"- 期权处理：权利金按 signed cash 记录；短期期权未平仓前不确认为 realized；到期/买平/卖平/指派时确认期权 PnL；指派交割通过链接表关联到底层股票交易。",
        f"- 基金处理：申购生成 fund lot，赎回按 FIFO 分配；期初基金按份额 * 期初净值生成 provisional opening lot；最新月份未进入期末持仓的申购标记为 `pending_settlement`。",
        f"- 股票短仓处理：卖空开仓和买回平仓按 signed cash 分配，realized PnL = 开仓现金 + 平仓现金。",
        "",
        "## 正股 / IPO Lot 概览",
        "",
        md_table(summary["lot_counts"], ["source_type", "cost_basis_status", "currency", "lot_count", "original_quantity", "remaining_quantity", "cost_basis_total"]),
        "",
        "## 正股 / IPO 已实现收益概览",
        "",
        md_table(summary["pnl_by_currency_status"], ["currency", "pnl_status", "allocation_count", "proceeds_total", "cost_total", "realized_pnl"]),
        "",
        "## 正股 / IPO 分标的已实现收益",
        "",
        md_table(summary["pnl_by_instrument"], ["instrument_key", "instrument_name", "currency", "quantity_sold", "proceeds_total", "cost_total", "realized_pnl", "pnl_status"], max_rows=80),
        "",
        "## 期权 Lot 概览",
        "",
        md_table(summary["option_lot_counts"], ["source_type", "position_side", "currency", "lot_count", "original_contracts", "remaining_contracts", "opening_net_cash_amount", "remaining_opening_cash_amount"]),
        "",
        "## 期权已实现收益概览",
        "",
        md_table(summary["option_pnl_by_currency"], ["currency", "pnl_status", "contracts_closed", "opening_cash_total", "closing_cash_total", "realized_pnl"]),
        "",
        "## 期权分合约已实现收益",
        "",
        md_table(summary["option_pnl_by_contract"], ["option_code", "underlying_symbol", "expiry_date", "strike_price", "option_type", "currency", "position_side", "close_outcome", "contracts_closed", "realized_pnl"], max_rows=80),
        "",
        "## 期权未平仓",
        "",
        md_table(summary["option_open_positions"], ["option_code", "underlying_symbol", "expiry_date", "strike_price", "option_type", "currency", "position_side", "remaining_contracts", "remaining_opening_cash_amount"], max_rows=80),
        "",
        "## 期权指派 / 行权底层链接",
        "",
        md_table(summary["option_underlying_links"], ["option_contract_key", "link_type", "underlying_event_id", "underlying_instrument_key", "underlying_quantity", "strike_price", "underlying_gross_amount", "confidence"], max_rows=80),
        "",
        "## 基金 Lot 概览",
        "",
        md_table(summary["fund_lot_counts"], ["source_type", "cost_basis_status", "settlement_status", "currency", "lot_count", "original_units", "remaining_units", "cost_basis_total", "remaining_cost"]),
        "",
        "## 基金已实现收益概览",
        "",
        md_table(summary["fund_pnl_by_currency"], ["currency", "pnl_status", "units_redeemed", "proceeds_total", "cost_total", "realized_pnl"]),
        "",
        "## 基金分标的已实现收益",
        "",
        md_table(summary["fund_pnl_by_fund"], ["fund_key", "fund_name", "currency", "units_redeemed", "proceeds_total", "cost_total", "realized_pnl", "pnl_status"], max_rows=80),
        "",
        "## 基金未平仓 / 待结算",
        "",
        md_table(summary["fund_open_positions"], ["fund_key", "fund_name", "currency", "remaining_units", "remaining_cost", "cost_basis_status"], max_rows=80),
        "",
        md_table(summary["fund_pending_positions"], ["fund_key", "fund_name", "currency", "remaining_units", "remaining_cost", "settlement_status"], max_rows=80),
        "",
        "## 股票短仓概览",
        "",
        md_table(summary["short_lot_counts"], ["currency", "lot_count", "original_quantity", "remaining_quantity", "opening_net_cash_amount", "remaining_opening_cash_amount"]),
        "",
        "## 股票短仓已实现收益",
        "",
        md_table(summary["short_pnl_by_currency"], ["currency", "pnl_status", "quantity_closed", "opening_cash_total", "closing_cash_total", "realized_pnl"]),
        "",
        md_table(summary["short_pnl_by_instrument"], ["instrument_key", "instrument_name", "currency", "quantity_closed", "opening_cash_total", "closing_cash_total", "realized_pnl", "pnl_status"], max_rows=80),
        "",
        "## 股票短仓未平仓",
        "",
        md_table(summary["short_open_positions"], ["instrument_key", "instrument_name", "currency", "remaining_quantity", "remaining_opening_cash_amount"], max_rows=80),
        "",
        "## 合并收益概览",
        "",
        md_table(summary["total_pnl_by_currency_status"], ["pnl_layer", "currency", "pnl_status", "realized_pnl"]),
        "",
        "## 校验概览",
        "",
        md_table(summary["validation_counts"], ["check_code", "status", "severity", "item_count"]),
        "",
        "## 暂缓处理范围",
        "",
        md_table(summary["deferred_items"], ["check_code", "severity", "item_count"]),
        "",
        "## 失败项",
        "",
        md_table(summary["failed_items"], ["check_code", "instrument_key", "source_table", "source_pk", "expected_value", "actual_value", "diff_value", "message"], max_rows=80),
        "",
        "## 导出文件",
        "",
        "- `lot-allocation-v1/position_lots.csv`",
        "- `lot-allocation-v1/lot_cost_components.csv`",
        "- `lot-allocation-v1/lot_allocations.csv`",
        "- `lot-allocation-v1/realized_pnl_by_instrument.csv`",
        "- `lot-allocation-v1/open_positions.csv`",
        "- `lot-allocation-v1/option_contract_lots.csv`",
        "- `lot-allocation-v1/option_lot_allocations.csv`",
        "- `lot-allocation-v1/option_realized_pnl_by_contract.csv`",
        "- `lot-allocation-v1/option_open_positions.csv`",
        "- `lot-allocation-v1/option_underlying_links.csv`",
        "- `lot-allocation-v1/fund_position_lots.csv`",
        "- `lot-allocation-v1/fund_lot_allocations.csv`",
        "- `lot-allocation-v1/fund_realized_pnl_by_fund.csv`",
        "- `lot-allocation-v1/fund_open_positions.csv`",
        "- `lot-allocation-v1/fund_pending_positions.csv`",
        "- `lot-allocation-v1/short_stock_lots.csv`",
        "- `lot-allocation-v1/short_stock_allocations.csv`",
        "- `lot-allocation-v1/short_stock_realized_pnl_by_instrument.csv`",
        "- `lot-allocation-v1/short_stock_open_positions.csv`",
        "- `lot-allocation-v1/total_realized_pnl_by_currency_status.csv`",
        "- `lot-allocation-v1/validation_items.csv`",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def status(args: argparse.Namespace) -> dict[str, Any]:
    with connect(args.db_path.resolve()) as conn:
        if not table_exists(conn, "lot_allocation_runs"):
            return {"status": "not_initialized", "db_path": str(args.db_path.resolve())}
        rows = rows_as_dicts(
            conn,
            """
            SELECT allocation_run_id, created_at, import_run_id, account_id, method, scope, status
            FROM lot_allocation_runs
            ORDER BY created_at DESC, allocation_run_id DESC
            LIMIT ?
            """,
            (args.limit,),
        )
    return {"status": "ok", "db_path": str(args.db_path.resolve()), "runs": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build FIFO lots and allocations for the investment database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run Lot / Allocation v1.")
    run_parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    run_parser.add_argument("--schema-path", type=Path, default=DEFAULT_SCHEMA)
    run_parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    run_parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--import-run-id", default=None)
    run_parser.add_argument("--account-id", default="futu_hk_main")
    run_parser.add_argument("--replace", action="store_true")
    run_parser.set_defaults(func=run_allocation)

    status_parser = subparsers.add_parser("status", help="List Lot / Allocation runs.")
    status_parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    status_parser.add_argument("--limit", type=int, default=10)
    status_parser.set_defaults(func=status)

    return parser


def main(argv: Iterable[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
