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
FUND_DUST_UNITS_TOLERANCE = Decimal("1")

DATE_RE = re.compile(r"(20\d{2})[/-](\d{2})[/-](\d{2})")
HK_CODE_AT_START_RE = re.compile(r"^\s*#?(\d{4,5})(?=\(|\b)")
HK_CODE_HASH_RE = re.compile(r"#(\d{4,5})")
US_SYMBOL_AT_START_RE = re.compile(r"^\s*([A-Z]{1,6})(?=\(|\b)")
OPTION_CODE_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d+", re.IGNORECASE)
OPTION_CODE_ANY_RE = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{3,7})", re.IGNORECASE)
FUND_CODE_RE = re.compile(r"(HK\d{10})", re.IGNORECASE)
NUMERIC_FUND_CODE_RE = re.compile(r"\b(8\d{5,6})\b")
HK_STRUCTURED_PRODUCT_SUFFIX_RE = re.compile(r"\.[CP](?:\b|$)", re.IGNORECASE)


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
    normalized = text(period)
    if len(normalized) == 4:
        return f"{normalized}-01-01"
    if len(normalized) >= 6:
        return f"{normalized[:4]}-{normalized[4:6]}-01"
    return normalized


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


def looks_like_hk_structured_product(*values: Any) -> bool:
    """Identify HK warrants/CBBC-like products that use stock-style numeric codes."""
    raw = " ".join(text(value) for value in values if text(value))
    if not raw:
        return False
    return bool(HK_STRUCTURED_PRODUCT_SUFFIX_RE.search(raw))


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
        inferred_type = "hk_structured_product" if looks_like_hk_structured_product(raw_joined, name) else "stock_or_etf"
        return Instrument(
            key=f"HK:{normalized}",
            code=normalized,
            name=name or None,
            market="HK",
            inferred_type=inferred_type,
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
    proceeds_policy: str = "provided"
    pnl_status_override: str | None = None


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
        numeric_match = NUMERIC_FUND_CODE_RE.search(part)
        if numeric_match:
            code = numeric_match.group(1)
            break
    if code is None:
        return None

    raw_name = text(name_raw) or text(code_raw)
    name = NUMERIC_FUND_CODE_RE.sub("", FUND_CODE_RE.sub("", raw_name)).strip()
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
    def __init__(
        self,
        conn: sqlite3.Connection,
        allocation_run_id: str,
        import_run_ids: Iterable[str],
        account_id: str,
        statement_ids: Iterable[str] | None = None,
    ):
        self.conn = conn
        self.run_id = allocation_run_id
        self.import_run_ids = tuple(import_run_ids)
        if not self.import_run_ids:
            raise ValueError("LotAllocator requires at least one import_run_id.")
        self.import_run_id = ",".join(self.import_run_ids)
        self.account_id = account_id
        self.statement_ids = tuple(sorted(set(statement_ids or [])))
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

    def fact_where_clause(self) -> tuple[str, tuple[Any, ...]]:
        import_placeholders = ", ".join("?" for _ in self.import_run_ids)
        clauses = [f"import_run_id IN ({import_placeholders})"]
        params: list[Any] = list(self.import_run_ids)
        if self.statement_ids:
            statement_placeholders = ", ".join("?" for _ in self.statement_ids)
            clauses.append(f"statement_id IN ({statement_placeholders})")
            params.extend(self.statement_ids)
        return " AND ".join(clauses), tuple(params)

    def first_period(self) -> str:
        where, params = self.fact_where_clause()
        row = self.conn.execute(f"SELECT MIN(period) FROM raw_statements WHERE {where}", params).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(f"No raw statements found for allocation scope: {self.import_run_id} / {self.account_id}")
        return row[0]

    def latest_period(self) -> str:
        where, params = self.fact_where_clause()
        row = self.conn.execute(f"SELECT MAX(period) FROM raw_statements WHERE {where}", params).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(f"No raw statements found for allocation scope: {self.import_run_id} / {self.account_id}")
        return row[0]

    def source_pk(self, row: sqlite3.Row, pk_col: str) -> str:
        raw_pk = text(row[pk_col])
        if len(self.import_run_ids) > 1:
            return f"{row['import_run_id']}:{raw_pk}"
        return raw_pk

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
        first_period = self.first_period()
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'opening'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY code_name, page, table_index, row_index
            """,
            (*params, first_period),
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

    def first_period_equity_quantity_changes(self, first_period: str) -> dict[str, Decimal]:
        where, params = self.fact_where_clause()
        changes: dict[str, Decimal] = {}

        trade_rows = self.conn.execute(
            f"""
            SELECT *
            FROM market_trades
            WHERE {where}
              AND period = ?
            ORDER BY trade_date, trade_id
            """,
            (*params, first_period),
        ).fetchall()
        for row in trade_rows:
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
            side = text(row["side"]).lower()
            position_effect = text(row["position_effect"]).lower()
            quantity = dec(row["quantity"])
            if side == "buy" and position_effect == "open":
                changes[inst.key] = changes.get(inst.key, Decimal("0")) + quantity
            elif side == "sell" and position_effect == "close":
                changes[inst.key] = changes.get(inst.key, Decimal("0")) - quantity

        movement_rows = self.conn.execute(
            f"""
            SELECT *
            FROM asset_movement_events
            WHERE {where}
              AND period = ?
              AND business_type = 'asset_movement'
              AND quantity IS NOT NULL
            ORDER BY event_date, asset_movement_id
            """,
            (*params, first_period),
        ).fetchall()
        for row in movement_rows:
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                name_raw=row["description_raw"],
                market="HK" if text(row["currency"]).upper() == "HKD" else None,
                currency=row["currency"],
            )
            if not inst.eligible_long_equity or not inst.key:
                continue
            changes[inst.key] = changes.get(inst.key, Decimal("0")) + dec(row["quantity"])

        return changes

    def load_account_inception_lots_from_first_ending(self) -> None:
        first_period = self.first_period()
        where, params = self.fact_where_clause()
        opening_rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'opening'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            """,
            (*params, first_period),
        ).fetchall()
        opening_keys: set[str] = set()
        for row in opening_rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if inst.eligible_long_equity and inst.key:
                opening_keys.add(inst.key)

        quantity_changes = self.first_period_equity_quantity_changes(first_period)
        ending_rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY code_name, page, table_index, row_index
            """,
            (*params, first_period),
        ).fetchall()
        for row in ending_rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            source_pk = f"{row['statement_id']}|{row['page']}|{row['table_index']}|{row['row_index']}"
            if not inst.eligible_long_equity or not inst.key:
                continue
            if inst.key in opening_keys:
                continue

            ending_quantity = dec(row["quantity"])
            net_change = quantity_changes.get(inst.key, Decimal("0"))
            inception_quantity = q6(ending_quantity - net_change)
            if inception_quantity <= Q6:
                continue

            if text(row["price"]):
                inferred_cost = q2(inception_quantity * dec(row["price"]))
            else:
                inferred_cost = q2(dec(row["market_value"]) * inception_quantity / ending_quantity)
            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=inst.key,
                instrument_code=inst.code,
                instrument_name=inst.name,
                market=inst.market,
                currency=text(row["currency"]).upper(),
                source_type="account_inception_position",
                source_table="position_snapshots",
                source_pk=source_pk,
                source_ref=None,
                open_date=period_start_date(first_period),
                settlement_date=None,
                original_quantity=inception_quantity,
                remaining_quantity=inception_quantity,
                remaining_cost=inferred_cost,
                cost_basis_total=inferred_cost,
                cost_basis_principal=inferred_cost,
                cost_basis_fee=Decimal("0"),
                cost_basis_status="provisional",
                cost_basis_source="first_statement_ending_market_value_inferred_account_inception",
                notes=(
                    "首份结单无期初持仓表；按首月期末持仓扣除首月净买入/净转入后的残量"
                    "生成账户初始 lot。成本按首月期末市值估算，只用于连续性和临时收益口径。"
                ),
            )
            self.add_component(
                lot,
                "account_inception_market_value",
                inferred_cost,
                "position_snapshots",
                source_pk,
                None,
                formula="first_period_inception_quantity * first_period_ending_price",
                notes="首张可用结单边界估算成本，非最终税务成本。",
            )
            self.add_lot(lot)
            self.add_validation(
                check_code="account_inception_position_lot",
                status="skipped",
                severity="warning",
                instrument_key=inst.key,
                source_table="position_snapshots",
                source_pk=source_pk,
                expected_value=ending_quantity,
                actual_value=inception_quantity,
                diff_value=net_change,
                message=f"{inst.key} 已根据首份结单期末持仓生成账户初始 lot。",
                notes="该 lot 解决数量连续性；真实历史成本如需税务级精度仍需人工确认。",
            )

    def first_seen_periods_by_instrument(self) -> dict[str, str]:
        where, params = self.fact_where_clause()
        first_seen: dict[str, str] = {}

        snapshot_rows = self.conn.execute(
            f"""
            SELECT period, code_name, market, currency, asset_category
            FROM position_snapshots
            WHERE {where}
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY period
            """,
            params,
        ).fetchall()
        for row in snapshot_rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if inst.eligible_long_equity and inst.key:
                period = text(row["period"])
                first_seen[inst.key] = min(first_seen.get(inst.key, period), period)

        trade_rows = self.conn.execute(
            f"""
            SELECT period, instrument_code_raw, instrument_symbol, instrument_name_raw, market, currency, instrument_type
            FROM market_trades
            WHERE {where}
              AND period IS NOT NULL
            ORDER BY period
            """,
            params,
        ).fetchall()
        for row in trade_rows:
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_symbol"],
                name_raw=row["instrument_name_raw"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["instrument_type"],
            )
            if inst.eligible_long_equity and inst.key:
                period = text(row["period"])
                first_seen[inst.key] = min(first_seen.get(inst.key, period), period)

        movement_rows = self.conn.execute(
            f"""
            SELECT period, instrument_code_raw, currency, description_raw
            FROM asset_movement_events
            WHERE {where}
              AND period IS NOT NULL
              AND quantity IS NOT NULL
            ORDER BY period
            """,
            params,
        ).fetchall()
        for row in movement_rows:
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                name_raw=row["description_raw"],
                market="HK" if text(row["currency"]).upper() == "HKD" else None,
                currency=row["currency"],
            )
            if inst.eligible_long_equity and inst.key:
                period = text(row["period"])
                first_seen[inst.key] = min(first_seen.get(inst.key, period), period)

        return first_seen

    def load_first_observed_opening_lots(self) -> None:
        first_period = self.first_period()
        first_seen = self.first_seen_periods_by_instrument()
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND snapshot_type = 'opening'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY period, code_name, page, table_index, row_index
            """,
            params,
        ).fetchall()
        loaded_keys: set[str] = set()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if not inst.eligible_long_equity or not inst.key:
                continue
            period = text(row["period"])
            if period == first_period:
                continue
            if first_seen.get(inst.key) != period:
                continue
            if inst.key in loaded_keys:
                continue
            quantity = dec(row["quantity"])
            market_value = q2(dec(row["market_value"]))
            source_pk = f"{row['statement_id']}|{row['page']}|{row['table_index']}|{row['row_index']}"
            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=inst.key,
                instrument_code=inst.code,
                instrument_name=inst.name,
                market=inst.market,
                currency=text(row["currency"]).upper(),
                source_type="first_observed_opening_position",
                source_table="position_snapshots",
                source_pk=source_pk,
                source_ref=None,
                open_date=period_start_date(period),
                settlement_date=None,
                original_quantity=quantity,
                remaining_quantity=quantity,
                remaining_cost=market_value,
                cost_basis_total=market_value,
                cost_basis_principal=market_value,
                cost_basis_fee=Decimal("0"),
                cost_basis_status="provisional",
                cost_basis_source="first_observed_statement_opening_market_value",
                notes="该标的首次出现在当前数据范围时已有期初持仓；按该期 opening 市值生成边界 lot。",
            )
            self.add_component(
                lot,
                "first_observed_opening_market_value",
                market_value,
                "position_snapshots",
                source_pk,
                None,
                notes="多账户/多市场分段接入时的边界估算成本，非最终税务成本。",
            )
            self.add_lot(lot)
            loaded_keys.add(inst.key)
            self.add_validation(
                check_code="first_observed_opening_position_lot",
                status="skipped",
                severity="warning",
                instrument_key=inst.key,
                source_table="position_snapshots",
                source_pk=source_pk,
                expected_value=quantity,
                actual_value=quantity,
                diff_value=Decimal("0"),
                message=f"{inst.key} 已根据首次出现期的 opening snapshot 生成边界 lot。",
                notes="该 lot 解决分段数据接入的数量连续性；真实买入成本如需税务级精度仍需人工确认。",
            )

    def handling_fee_rows_for_ipo(self, hk_code: str) -> list[sqlite3.Row]:
        where, params = self.fact_where_clause()
        return self.conn.execute(
            f"""
            SELECT import_run_id, cash_entry_id, amount, source_refs
            FROM cash_ledger_entries
            WHERE {where}
              AND business_type = 'ipo_subscription'
              AND cash_leg_type = 'application_handling_fee'
              AND description LIKE ?
            ORDER BY period, event_date, cash_entry_id
            """,
            (*params, f"%#{hk_code}%"),
        ).fetchall()

    def annual_ipo_cash_rows(self, hk_code: str) -> list[sqlite3.Row]:
        where, params = self.fact_where_clause()
        return self.conn.execute(
            f"""
            SELECT import_run_id, cash_entry_id, amount, source_refs, description
            FROM cash_ledger_entries
            WHERE {where}
              AND description LIKE ?
              AND (
                description LIKE '%IPO Application Amount%'
                OR description LIKE '%IPO Application Handling Fee%'
                OR description LIKE '%IPO Refund Amount%'
              )
            ORDER BY period, event_date, cash_entry_id
            """,
            (*params, f"%#{hk_code}%"),
        ).fetchall()

    def derived_annual_ipo_cost(self, hk_code: str) -> tuple[Decimal, str | None, str | None]:
        rows = self.annual_ipo_cash_rows(hk_code)
        net_cash = q2(sum((dec(row["amount"]) for row in rows), Decimal("0")))
        if net_cash >= 0:
            return Decimal("0"), None, None
        source_pk = ",".join(self.source_pk(row, "cash_entry_id") for row in rows)
        source_refs = ",".join(text(row["source_refs"]) for row in rows if text(row["source_refs"]))
        return abs(net_cash), source_pk or None, source_refs or None

    def load_ipo_allotment_lots(self) -> None:
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM asset_movement_events
            WHERE {where}
              AND (
                (
                  business_type = 'ipo_subscription'
                  AND asset_movement_type = 'allotment'
                )
                OR description_raw LIKE 'IPO Allotment Qty%'
              )
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY event_date, asset_movement_id
            """,
            params,
        ).fetchall()
        for row in rows:
            source_pk = self.source_pk(row, "asset_movement_id")
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
                    source_pk=source_pk,
                    message=f"IPO 中签配发无法识别标的：{row['description_raw']}",
                )
                continue

            quantity = dec(row["quantity"])
            if row["amount"] is not None and dec(row["amount"]) > 0:
                principal = q2(dec(row["amount"]))
                handling_fee_rows = self.handling_fee_rows_for_ipo(inst.code)
                handling_fee = q2(sum((abs(dec(fee_row["amount"])) for fee_row in handling_fee_rows), Decimal("0")))
                hidden_fee = q2(principal * IPO_ALLOTMENT_FEE_RATE)
                total_cost = q2(principal + handling_fee + hidden_fee)
                cost_basis_source = "ipo_allotment_amount_plus_explicit_handling_fee_plus_formula_fee"
            else:
                principal, annual_cash_source_pk, annual_cash_source_refs = self.derived_annual_ipo_cost(inst.code)
                handling_fee_rows = []
                handling_fee = Decimal("0")
                hidden_fee = Decimal("0")
                total_cost = principal
                cost_basis_source = "annual_ipo_cash_legs_net_cost"
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
                source_pk=source_pk,
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
                cost_basis_source=cost_basis_source,
                notes="IPO 中签 lot；年度账单缺配发金额时使用 IPO 申购款、手续费、退款现金腿净额。",
            )
            if row["amount"] is not None and dec(row["amount"]) > 0:
                self.add_component(lot, "ipo_allotment_principal", principal, "asset_movement_events", source_pk, row["source_ref"])
            elif principal:
                self.add_component(
                    lot,
                    "annual_ipo_net_cash_cost",
                    principal,
                    "cash_ledger_entries",
                    annual_cash_source_pk,
                    annual_cash_source_refs,
                    notes="年度账单未给配发金额，使用申购款 + 申购费 - 退款的净现金成本。",
                )
            if handling_fee:
                self.add_component(
                    lot,
                    "application_handling_fee",
                    handling_fee,
                    "cash_ledger_entries",
                    ",".join(self.source_pk(fee_row, "cash_entry_id") for fee_row in handling_fee_rows),
                    ",".join(text(fee_row["source_refs"]) for fee_row in handling_fee_rows if text(fee_row["source_refs"])),
                )
            else:
                self.add_validation(
                    check_code="ipo_handling_fee_absent",
                    status="passed",
                    severity="info",
                    instrument_key=inst.key,
                    source_table="asset_movement_events",
                    source_pk=source_pk,
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
                source_pk,
                row["source_ref"],
                formula="allotment_amount * 0.010085",
                notes="富途 IPO 中签 1% 手续费 + 约 0.0085% 小额市场/政府费用的合并口径。",
            )
            self.add_lot(lot)

    def classify_asset_movement_lot(self, description: str) -> tuple[str, str]:
        upper = description.upper()
        if "STOCK DIVIDEND" in upper or "DISTRIBUTION IN SPECIE" in upper:
            return "corporate_action_stock_distribution", "corporate_action_asset_movement_cost_unknown"
        if "SUBSCRIPTION RIGHTS" in upper:
            return "corporate_action_subscription_rights_settlement", "corporate_action_rights_cost_unknown"
        if "RSU" in upper:
            return "employee_equity_rsu_vest", "employee_equity_fmv_pending"
        if "GIFT STOCK" in upper:
            return "broker_reward_stock", "broker_reward_cost_unknown"
        if upper.strip() == "SI IN" or " SI IN" in upper:
            return "external_asset_transfer_in", "external_transfer_cost_unknown"
        return "asset_movement_in", "asset_movement_cost_unknown"

    def rsu_offer_key(self, description: str) -> str | None:
        match = re.search(r"(T-RSU-Offer-\d+)", text(description), re.IGNORECASE)
        return match.group(1) if match else None

    def manual_rsu_vesting_cost(self, source_pk: str, description: str) -> dict[str, Any] | None:
        offer_key = self.rsu_offer_key(description)
        filters = ["me.source_ref = ?"]
        params: list[Any] = [source_pk]
        if offer_key:
            filters.append("me.description LIKE ?")
            params.append(f"%{offer_key}%")
        row = self.conn.execute(
            f"""
            SELECT
              me.manual_event_id,
              me.event_date AS vesting_date,
              me.amount AS total_cost,
              me.description,
              me.source_label,
              me.source_ref,
              me.notes AS event_notes,
              ml.quantity AS net_quantity,
              ml.unit_price AS unit_price,
              ml.amount AS leg_amount,
              ml.notes AS leg_notes
            FROM manual_events me
            JOIN manual_event_legs ml
              ON ml.manual_event_id = me.manual_event_id
             AND ml.leg_role = 'net_shares_received'
            WHERE me.status = 'active'
              AND me.business_type = 'employee_equity'
              AND me.event_subtype = 'rsu_vest'
              AND ({' OR '.join(filters)})
            ORDER BY me.event_date, me.manual_event_id
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        quantity = q6(dec(row["net_quantity"]))
        total = q2(dec(row["total_cost"]) if text(row["total_cost"]) else dec(row["leg_amount"]))
        unit_price = dec(row["unit_price"])
        notes = (
            f"RSU 成本已按用户确认到账日口径补录：到账数量 {decimal_key(quantity)} * "
            f"腾讯控股收盘价 {decimal_key(unit_price)}。{text(row['event_notes'])}"
        ).strip()
        return {
            "quantity": quantity,
            "total": total,
            "principal": total,
            "fee": Decimal("0"),
            "status": "final",
            "source": "user_input_rsu_arrival_date_close_price",
            "notes": notes,
            "components": [
                (
                    "rsu_vesting_fmv_cost",
                    total,
                    "manual_events",
                    row["manual_event_id"],
                    row["source_label"],
                    "net_shares_received * close_price",
                    row["leg_notes"] or "RSU 到账日收盘价成本。",
                )
            ],
            "validation": {
                "check_code": "rsu_vesting_manual_cost_applied",
                "source_table": "manual_events",
                "source_pk": row["manual_event_id"],
                "expected_value": None,
                "actual_value": quantity,
                "diff_value": None,
                "message": f"{source_pk} 已应用 RSU 人工归属成本。",
                "notes": notes,
            },
        }

    def has_manual_rsu_vesting_for_offer(self, description: str) -> bool:
        offer_key = self.rsu_offer_key(description)
        if not offer_key:
            return False
        row = self.conn.execute(
            """
            SELECT 1
            FROM manual_events
            WHERE status = 'active'
              AND business_type = 'employee_equity'
              AND event_subtype = 'rsu_vest'
              AND description LIKE ?
            LIMIT 1
            """,
            (f"%{offer_key}%",),
        ).fetchone()
        return row is not None

    def classify_asset_movement_removal(self, row: sqlite3.Row, inst: Instrument | None = None) -> tuple[str, str | None, str]:
        description = text(row["description_raw"])
        upper = description.upper()
        if "ACCOUNT UPGRADE" in upper:
            return "skip", None, "账户升级在 owner 级口径下不改变总持仓，忽略同日账户间搬移。"
        if "REVERSE IPO ALLOTMENT" in upper or text(row["business_type"]) == "ipo_subscription":
            return "provided", None, "IPO 中签撤回 / 取消上市，按资产反向流水关闭中签 lot。"
        if "SUBSCRIPTION RIGHTS" in upper:
            return "skip", None, "供股权/认购权自身的减少只是权利结转；最终落股成本已在目标股票 lot 中按认购现金和手续费确认。"
        if (inst and inst.inferred_type == "hk_structured_product") or "HOLDING_REMOVAL" in upper or "PAYMENT FOLLOWING MCE" in upper:
            return "provided", None, "港股权证/牛熊证到期、强赎或失效，按同日同代码净现金腿作为 proceeds；无现金腿则按 0 proceeds 归零。"
        if upper.strip() == "SI OUT" or " SI OUT" in upper:
            return "cost_basis_transfer", "non_taxable_transfer", "外部转出只迁移持仓和成本，不确认为市场卖出收益。"
        if "RSU TAX STOCK" in upper:
            if self.has_manual_rsu_vesting_for_offer(description):
                return "skip", None, "该 RSU 事件已按到账数量建 lot，扣税股不再单独关闭 lot。"
            return "cost_basis_transfer", "non_taxable_withholding", "RSU 扣税股只移出投资持仓，不确认为市场卖出收益。"
        if text(row["asset_movement_type"]).lower() in {"withdrawal", "out"}:
            return "cost_basis_transfer", "non_taxable_transfer", "资产减少流水按成本迁移处理，不确认为市场卖出收益。"
        return "cost_basis_transfer", "non_taxable_transfer", "未细分的资产减少流水先按成本迁移处理，保留原始备注供复核。"

    def asset_movement_removal_cash_proceeds(self, row: sqlite3.Row, inst: Instrument) -> Decimal:
        if not inst.code:
            return Decimal("0")
        event_date = normalize_date(row["event_date"])
        cash_rows = self.conn.execute(
            """
            SELECT event_date, amount
            FROM cash_ledger_entries
            WHERE import_run_id = ?
              AND statement_id = ?
              AND description LIKE ?
            ORDER BY event_date, cash_entry_id
            """,
            (row["import_run_id"], row["statement_id"], f"%{inst.code.lstrip('0') or inst.code}%"),
        ).fetchall()
        total = Decimal("0")
        for cash_row in cash_rows:
            cash_date = normalize_date(cash_row["event_date"])
            if event_date and cash_date and cash_date != event_date:
                continue
            total += dec(cash_row["amount"])
        return q2(total)

    def cash_rows_for_subscription_rights(self, row: sqlite3.Row, target_code: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        code = target_code.lstrip("0") or target_code
        final_date = normalize_date(row["event_date"])
        start_date = final_date
        scrip_row = self.conn.execute(
            """
            SELECT event_date
            FROM asset_movement_events
            WHERE import_run_id = ?
              AND statement_id = ?
              AND description_raw LIKE 'Scrip Issued by%'
              AND description_raw LIKE ?
            ORDER BY event_date
            LIMIT 1
            """,
            (row["import_run_id"], row["statement_id"], f"%{code}%"),
        ).fetchone()
        if scrip_row is not None:
            start_date = normalize_date(scrip_row["event_date"]) or start_date

        cash_rows = self.conn.execute(
            """
            SELECT import_run_id, cash_entry_id, event_date, amount, source_refs, description
            FROM cash_ledger_entries
            WHERE import_run_id = ?
              AND statement_id = ?
              AND amount < 0
            ORDER BY event_date, cash_entry_id
            """,
            (row["import_run_id"], row["statement_id"]),
        ).fetchall()
        principal_rows: list[sqlite3.Row] = []
        fee_rows: list[sqlite3.Row] = []
        for cash_row in cash_rows:
            cash_date = normalize_date(cash_row["event_date"])
            if start_date and cash_date and cash_date < start_date:
                continue
            if final_date and cash_date and cash_date > final_date:
                continue
            description = text(cash_row["description"]).upper()
            if "SUBSCRIPTION RIGHTS AMOUNT" in description and code in description:
                principal_rows.append(cash_row)
            elif description.startswith("HANDLING CHARGE") and code in description and "SUBSCRIPTION" not in description:
                fee_rows.append(cash_row)
        return principal_rows, fee_rows

    def parent_distribution_terms(self, description: str) -> tuple[str | None, Decimal | None]:
        upper = description.upper()
        match = re.search(r"(\d{3,5})\.HK\s+STOCK DIVIDEND\s+(\d{3,5})\.HK\s+1\s+FOR\s+(\d+)", upper)
        if match:
            return match.group(1).zfill(5), Decimal(match.group(3))
        if "DISTRIBUTION IN SPECIE" in upper and "JD.COM" in upper:
            ratio_match = re.search(r"EVERY\s+(\d+)", upper)
            return "00700", Decimal(ratio_match.group(1)) if ratio_match else Decimal("21")
        return None, None

    def snapshot_anchor_date(self, period: str, snapshot_type: str) -> str | None:
        period = text(period)
        snapshot_type = text(snapshot_type).lower()
        if re.fullmatch(r"\d{4}", period):
            return f"{period}-01-01" if snapshot_type == "opening" else f"{period}-12-31"
        if re.fullmatch(r"\d{6}", period):
            year = int(period[:4])
            month = int(period[4:6])
            if snapshot_type == "opening":
                return f"{year:04d}-{month:02d}-01"
            if month == 12:
                return f"{year:04d}-12-31"
            next_month = datetime(year, month + 1, 1)
            end_day = (next_month - datetime.resolution).day
            return f"{year:04d}-{month:02d}-{end_day:02d}"
        return None

    def latest_position_quantity(self, code: str, event_date: str | None) -> Decimal | None:
        if not event_date:
            return None
        normalized_code = code.zfill(5) if code.isdigit() else code.upper()
        rows = self.conn.execute(
            """
            SELECT period, snapshot_type, code_name, quantity
            FROM position_snapshots
            WHERE quantity IS NOT NULL
              AND (
                code_name LIKE ?
                OR code_name LIKE ?
              )
            ORDER BY period, snapshot_type
            """,
            (f"%{normalized_code}%", f"%{normalized_code.lstrip('0')}%"),
        ).fetchall()
        best_date: str | None = None
        best_quantity: Decimal | None = None
        for snap in rows:
            snap_date = self.snapshot_anchor_date(snap["period"], snap["snapshot_type"])
            if not snap_date or snap_date > event_date:
                continue
            if best_date is None or snap_date >= best_date:
                best_date = snap_date
                best_quantity = dec(snap["quantity"])
        return best_quantity

    def residual_rows_for_distribution(self, row: sqlite3.Row) -> list[sqlite3.Row]:
        description = text(row["description_raw"]).upper()
        if "DISTRIBUTION IN SPECIE" not in description:
            return []
        return self.conn.execute(
            """
            SELECT import_run_id, cash_entry_id, amount, source_refs, description
            FROM cash_ledger_entries
            WHERE import_run_id = ?
              AND statement_id = ?
              AND amount > 0
              AND description LIKE '%Residual Value%'
              AND description LIKE '%Distribution in Specie%'
            ORDER BY event_date, cash_entry_id
            """,
            (row["import_run_id"], row["statement_id"]),
        ).fetchall()

    def nearest_trade_price_for_instrument(self, instrument_code: str, event_date: str | None) -> tuple[Decimal | None, str | None, str | None]:
        if not event_date:
            return None, None, None
        code = instrument_code.zfill(5) if instrument_code.isdigit() else instrument_code.upper()
        rows = self.conn.execute(
            """
            SELECT import_run_id, trade_id, trade_date, price, source_refs
            FROM market_trades
            WHERE price IS NOT NULL
              AND (
                instrument_symbol = ?
                OR instrument_code_raw = ?
                OR instrument_code_raw = ?
              )
            ORDER BY trade_date, trade_id
            """,
            (code, code, code.lstrip("0")),
        ).fetchall()
        best: tuple[int, sqlite3.Row] | None = None
        event_dt = datetime.fromisoformat(event_date)
        for trade in rows:
            trade_date = normalize_date(trade["trade_date"])
            if not trade_date:
                continue
            delta = abs((datetime.fromisoformat(trade_date) - event_dt).days)
            if delta > 7:
                continue
            if best is None or delta < best[0]:
                best = (delta, trade)
        if best is None:
            return None, None, None
        trade = best[1]
        return dec(trade["price"]), self.source_pk(trade, "trade_id"), text(trade["source_refs"]) or None

    def derive_asset_movement_cost(
        self,
        row: sqlite3.Row,
        inst: InstrumentInfo,
        source_type: str,
        quantity: Decimal,
        event_date: str | None,
    ) -> dict[str, Any]:
        if source_type == "employee_equity_rsu_vest":
            manual_cost = self.manual_rsu_vesting_cost(
                self.source_pk(row, "asset_movement_id"),
                text(row["description_raw"]),
            )
            if manual_cost is not None:
                return manual_cost

        if source_type == "corporate_action_subscription_rights_settlement" and inst.code:
            principal_rows, fee_rows = self.cash_rows_for_subscription_rights(row, inst.code)
            principal = q2(sum((abs(dec(cash_row["amount"])) for cash_row in principal_rows), Decimal("0")))
            fee = q2(sum((abs(dec(cash_row["amount"])) for cash_row in fee_rows), Decimal("0")))
            if principal or fee:
                return {
                    "total": q2(principal + fee),
                    "principal": principal,
                    "fee": fee,
                    "status": "final",
                    "source": "cash_paid_plus_direct_fee",
                    "notes": f"供股/配股落股成本按现金认购款和直接手续费资本化。raw={row['description_raw']}",
                    "components": [
                        (
                            "subscription_rights_cash_paid",
                            principal,
                            "cash_ledger_entries",
                            ",".join(self.source_pk(cash_row, "cash_entry_id") for cash_row in principal_rows),
                            ",".join(text(cash_row["source_refs"]) for cash_row in principal_rows if text(cash_row["source_refs"])),
                            None,
                            "供股/配股现金认购款。",
                        ),
                        (
                            "subscription_rights_direct_fee",
                            fee,
                            "cash_ledger_entries",
                            ",".join(self.source_pk(cash_row, "cash_entry_id") for cash_row in fee_rows),
                            ",".join(text(cash_row["source_refs"]) for cash_row in fee_rows if text(cash_row["source_refs"])),
                            None,
                            "供股/配股相关直接手续费。",
                        ),
                    ],
                }

        if source_type == "corporate_action_stock_distribution":
            parent_code, ratio = self.parent_distribution_terms(text(row["description_raw"]))
            residual_rows = self.residual_rows_for_distribution(row)
            residual_cash = q2(sum((dec(cash_row["amount"]) for cash_row in residual_rows), Decimal("0")))
            parent_quantity = self.latest_position_quantity(parent_code, event_date) if parent_code and ratio else None
            if residual_cash > 0 and parent_quantity and ratio and quantity > 0:
                full_entitlement = parent_quantity / ratio
                whole_shares = Decimal(int(full_entitlement))
                fractional_share = full_entitlement - whole_shares
                if fractional_share > 0:
                    unit_fmv = residual_cash / fractional_share
                    total = q2(unit_fmv * quantity)
                    return {
                        "total": total,
                        "principal": total,
                        "fee": Decimal("0"),
                        "status": "final",
                        "source": "fair_value_from_fractional_cash_residual",
                        "notes": (
                            f"实物分派按 fractional residual cash 反推分派日公允价值；"
                            f"parent={parent_code}, ratio=1:{decimal_key(ratio)}, parent_qty={decimal_key(parent_quantity)}。"
                        ),
                        "components": [
                            (
                                "distribution_in_specie_fair_value",
                                total,
                                "cash_ledger_entries",
                                ",".join(self.source_pk(cash_row, "cash_entry_id") for cash_row in residual_rows),
                                ",".join(text(cash_row["source_refs"]) for cash_row in residual_rows if text(cash_row["source_refs"])),
                                "residual_cash / fractional_share * whole_shares_received",
                                "用现金残值反推同一分派事件的全股公允价值。",
                            )
                        ],
                    }

            if inst.code:
                proxy_price, proxy_source_pk, proxy_source_ref = self.nearest_trade_price_for_instrument(inst.code, event_date)
                if proxy_price is not None:
                    total = q2(proxy_price * quantity)
                    return {
                        "total": total,
                        "principal": total,
                        "fee": Decimal("0"),
                        "status": "provisional",
                        "source": "nearby_trade_price_fmv_proxy",
                        "notes": (
                            f"实物分派缺少分派日 FMV；暂用 7 天内同标的交易价 {decimal_key(proxy_price)} "
                            f"作为估算成本，后续应替换为分派日官方/行情收盘价。raw={row['description_raw']}"
                        ),
                        "components": [
                            (
                                "distribution_in_specie_fmv_proxy",
                                total,
                                "market_trades",
                                proxy_source_pk,
                                proxy_source_ref,
                                "nearby_trade_price * shares_received",
                                "估算锚点，不进入 final 税务口径。",
                            )
                        ],
                        "warning": "corporate_action_fmv_proxy_lot",
                    }

        if source_type == "external_asset_transfer_in":
            return {
                "total": Decimal("0"),
                "principal": Decimal("0"),
                "fee": Decimal("0"),
                "status": "provisional",
                "source": "external_transfer_carryover_basis_pending",
                "notes": f"外部转入应沿用来源账户历史成本；当前缺来源成本，按待补成本标记。raw={row['description_raw']}",
                "components": [],
                "warning": "external_transfer_carryover_basis_pending",
            }

        if source_type == "broker_reward_stock":
            return {
                "total": Decimal("0"),
                "principal": Decimal("0"),
                "fee": Decimal("0"),
                "status": "provisional",
                "source": "broker_reward_fair_value_pending",
                "notes": f"券商赠股通常应按入账日公允价值确认成本/奖励收入；当前缺 FMV 锚点，按待补标记。raw={row['description_raw']}",
                "components": [],
                "warning": "broker_reward_fair_value_pending",
            }

        return {
            "total": Decimal("0"),
            "principal": Decimal("0"),
            "fee": Decimal("0"),
            "status": "provisional",
            "source": "asset_movement_cost_unknown",
            "notes": f"资产增加事件有数量但无金额；成本待通过公司行动规则、外部转入成本或人工补录确认。raw={row['description_raw']}",
            "components": [],
            "warning": "asset_movement_cost_unknown_lot",
        }

    def load_asset_movement_lots(self) -> None:
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM asset_movement_events
            WHERE {where}
              AND business_type = 'asset_movement'
              AND direction_raw IN ('增加', 'In', 'IN')
              AND quantity IS NOT NULL
              AND quantity > 0
              AND description_raw NOT LIKE 'Account Upgrade%'
              AND description_raw NOT LIKE 'IPO Allotment Qty%'
              AND description_raw NOT LIKE 'Scrip Issued by%'
            ORDER BY event_date, asset_movement_id
            """,
            params,
        ).fetchall()
        for row in rows:
            source_pk = self.source_pk(row, "asset_movement_id")
            source_type, cost_source = self.classify_asset_movement_lot(text(row["description_raw"]))
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                name_raw=row["description_raw"],
                market="HK" if text(row["currency"]).upper() == "HKD" else None,
                currency=row["currency"],
            )
            if not inst.eligible_long_equity or not inst.key:
                self.add_validation(
                    check_code="asset_movement_lot_unclassified",
                    status="skipped",
                    severity="warning",
                    source_table="asset_movement_events",
                    source_pk=source_pk,
                    message=f"资产增加事件暂未生成 lot：{row['instrument_code_raw']} {row['description_raw']}",
                    notes=inst.reason,
                )
                continue
            quantity = dec(row["quantity"])
            event_date = normalize_date(row["event_date"])
            cost = self.derive_asset_movement_cost(row, inst, source_type, quantity, event_date)
            raw_quantity = quantity
            if "quantity" in cost:
                quantity = q6(dec(cost["quantity"]))
            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=inst.key,
                instrument_code=inst.code,
                instrument_name=inst.name,
                market=inst.market,
                currency=text(row["currency"]).upper(),
                source_type=source_type,
                source_table="asset_movement_events",
                source_pk=source_pk,
                source_ref=row["source_ref"],
                open_date=event_date,
                settlement_date=None,
                original_quantity=quantity,
                remaining_quantity=quantity,
                remaining_cost=cost["total"],
                cost_basis_total=cost["total"],
                cost_basis_principal=cost["principal"],
                cost_basis_fee=cost["fee"],
                cost_basis_status=cost["status"],
                cost_basis_source=cost["source"] or cost_source,
                notes=cost["notes"],
            )
            if cost["components"]:
                for component_type, amount, source_table, component_source_pk, source_ref, formula, notes in cost["components"]:
                    if amount:
                        self.add_component(lot, component_type, amount, source_table, component_source_pk, source_ref, formula=formula, notes=notes)
            else:
                self.add_component(
                    lot,
                    "asset_movement_cost_placeholder",
                    Decimal("0"),
                    "asset_movement_events",
                    source_pk,
                    row["source_ref"],
                    notes="金额缺失；仅用于数量连续性，成本仍待确认。",
                )
            self.add_lot(lot)
            validation = cost.get("validation")
            if validation:
                self.add_validation(
                    check_code=validation["check_code"],
                    status="passed",
                    severity="info",
                    instrument_key=inst.key,
                    source_table=validation["source_table"],
                    source_pk=validation["source_pk"],
                    expected_value=raw_quantity,
                    actual_value=validation.get("actual_value"),
                    diff_value=q6(raw_quantity - validation.get("actual_value", quantity)),
                    message=validation["message"],
                    notes=validation.get("notes"),
                )
            warning = cost.get("warning")
            if warning:
                self.add_validation(
                    check_code=warning,
                    status="skipped",
                    severity="warning",
                    instrument_key=inst.key,
                    source_table="asset_movement_events",
                    source_pk=source_pk,
                    expected_value=cost["total"] if cost["total"] else None,
                    actual_value=quantity,
                    message=f"{inst.key} 已根据资产增加事件生成 {cost['status']} lot，成本口径仍需确认或补全。",
                    notes=cost["notes"],
                )

    def load_asset_movement_removals(self) -> None:
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM asset_movement_events
            WHERE {where}
              AND business_type IN ('asset_movement', 'ipo_subscription')
              AND quantity IS NOT NULL
              AND quantity < 0
            ORDER BY event_date, asset_movement_id
            """,
            params,
        ).fetchall()
        for row in rows:
            source_pk = self.source_pk(row, "asset_movement_id")
            inst = infer_instrument(
                code_raw=row["instrument_code_raw"],
                symbol=row["instrument_code_raw"],
                name_raw=row["description_raw"],
                market="HK" if text(row["currency"]).upper() == "HKD" else None,
                currency=row["currency"],
            )
            proceeds_policy, pnl_status_override, notes = self.classify_asset_movement_removal(row, inst)
            if proceeds_policy == "skip":
                self.add_validation(
                    check_code="asset_movement_removal_skipped",
                    status="skipped",
                    severity="info",
                    source_table="asset_movement_events",
                    source_pk=source_pk,
                    actual_value=dec(row["quantity"]),
                    message=f"资产减少流水已按规则跳过：{row['description_raw']}",
                    notes=notes,
                )
                continue
            if not inst.eligible_long_equity or not inst.key:
                self.add_validation(
                    check_code="asset_movement_removal_unclassified",
                    status="skipped",
                    severity="warning",
                    source_table="asset_movement_events",
                    source_pk=source_pk,
                    message=f"资产减少事件暂未进入 allocation：{row['instrument_code_raw']} {row['description_raw']}",
                    notes=inst.reason,
                )
                continue

            description_upper = text(row["description_raw"]).upper()
            proceeds = Decimal("0")
            if proceeds_policy == "provided" and (
                inst.inferred_type == "hk_structured_product"
                or "HOLDING_REMOVAL" in description_upper
                or "PAYMENT FOLLOWING MCE" in description_upper
            ):
                proceeds = self.asset_movement_removal_cash_proceeds(row, inst)

            self.close_events.append(
                CloseEvent(
                    event_id=source_pk,
                    event_table="asset_movement_events",
                    event_date=normalize_date(row["event_date"]),
                    settlement_date=None,
                    source_ref=row["source_ref"],
                    instrument_key=inst.key,
                    instrument_code=inst.code,
                    instrument_name=inst.name,
                    currency=text(row["currency"]).upper(),
                    quantity=abs(dec(row["quantity"])),
                    proceeds=proceeds,
                    notes=f"{notes} raw={row['description_raw']}",
                    proceeds_policy=proceeds_policy,
                    pnl_status_override=pnl_status_override,
                )
            )

    def normalized_trade_dates(self, row: sqlite3.Row) -> tuple[str | None, str | None, str | None]:
        dates = extract_dates(row["trade_date"], row["settlement_date"], row["instrument_code_raw"], row["instrument_symbol"])
        trade_date = normalize_date(row["trade_date"]) or (dates[0] if dates else None)
        settlement_date = normalize_date(row["settlement_date"]) or (dates[1] if len(dates) > 1 else None)
        note = None
        if not row["trade_date"] and trade_date:
            note = "交易日期由原始交易文本补抽。"
        return trade_date, settlement_date, note

    def load_market_trade_lots_and_closes(self) -> None:
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM market_trades
            WHERE {where}
            ORDER BY period, COALESCE(trade_date, ''), trade_id
            """,
            params,
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
            source_pk = self.source_pk(row, "trade_id")
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
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM market_trades
            WHERE {where}
            ORDER BY period, COALESCE(trade_date, ''), trade_id
            """,
            params,
        ).fetchall()
        for row in rows:
            source_pk = self.source_pk(row, "trade_id")
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
                        source_pk=source_pk,
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
                        source_pk=source_pk,
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
                        event_id=source_pk,
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
                        event_id=source_pk,
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
                    source_pk=source_pk,
                    message=f"期权交易方向暂无法处理：{contract.code} {side}/{position_effect}",
                )

    def load_option_exercise_events(self) -> None:
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM asset_movement_events
            WHERE {where}
              AND asset_movement_type = 'option_expiry_close'
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY event_date, asset_movement_id
            """,
            params,
        ).fetchall()
        for row in rows:
            source_pk = self.source_pk(row, "asset_movement_id")
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
                    source_pk=source_pk,
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
                    event_id=source_pk,
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

        where, params = self.fact_where_clause()
        candidate_rows = self.conn.execute(
            f"""
            SELECT *
            FROM market_trades
            WHERE {where}
              AND REPLACE(trade_date, '/', '-') = ?
              AND side = ?
              AND position_effect = ?
              AND currency = ?
            ORDER BY trade_id
            """,
            (*params, event.event_date, expected_side, expected_effect, event.contract.currency),
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
                "underlying_event_id": self.source_pk(matched, "trade_id"),
                "underlying_instrument_key": matched_inst.key,
                "underlying_quantity": expected_quantity,
                "strike_price": event.contract.strike_price,
                "underlying_gross_amount": q2(abs(dec(matched["gross_amount"]))),
                "confidence": "inferred_same_date_qty_strike",
                "notes": f"{event.contract.code} -> {matched_inst.key} {self.source_pk(matched, 'trade_id')}",
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
        latest_period = self.latest_period()
        expected: dict[str, Decimal] = {}
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
            """,
            (*params, latest_period),
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
        first_period = self.first_period()
        where, params = self.fact_where_clause()
        opening_rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'opening'
              AND asset_category = 'fund'
              AND quantity IS NOT NULL
              AND quantity > 0
            ORDER BY code_name, page, table_index, row_index
            """,
            (*params, first_period),
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
            f"""
            SELECT *
            FROM fund_orders
            WHERE {where}
            ORDER BY period, COALESCE(trade_date, order_date, ''), fund_order_id
            """,
            params,
        ).fetchall()
        for row in order_rows:
            source_pk = self.source_pk(row, "fund_order_id")
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
                    source_pk=source_pk,
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
                    source_pk=source_pk,
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
                        source_pk=source_pk,
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
                        order_id=source_pk,
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
                    source_pk=source_pk,
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
                synthetic_cost = q2(remaining_proceeds)
                synthetic_lot = FundLot(
                    fund_lot_id=self.next_id("fund_lot"),
                    account_id=self.account_id,
                    fund=event.fund,
                    source_type="synthetic_missing_fund_opening_position",
                    source_table="fund_orders",
                    source_pk=event.order_id,
                    source_ref=event.source_ref,
                    open_date=event.redemption_date,
                    original_units=remaining_units,
                    remaining_units=remaining_units,
                    cost_basis_total=synthetic_cost,
                    remaining_cost=synthetic_cost,
                    cost_basis_status="provisional",
                    cost_basis_source="missing_pre_scope_fund_lot_zero_pnl_placeholder",
                    cash_source_status=event.cash_match_status,
                    notes="范围开始前缺少基金申购 / 期初份额成本；临时按本次赎回 proceeds 生成占位 lot，使该段 realized PnL 为 0。",
                )
                self.fund_lots.append(synthetic_lot)
                lots_by_key.setdefault(event.fund.key, []).append(synthetic_lot)
                self.fund_allocations.append(
                    {
                        "fund_allocation_id": self.next_id("fund_allocation"),
                        "fund_lot_id": synthetic_lot.fund_lot_id,
                        "redemption_order_id": event.order_id,
                        "redemption_date": event.redemption_date,
                        "redemption_source_ref": event.source_ref,
                        "fund_key": event.fund.key,
                        "fund_code": event.fund.code,
                        "fund_name": event.fund.name,
                        "currency": event.fund.currency,
                        "units_allocated": remaining_units,
                        "proceeds_allocated": remaining_proceeds,
                        "cost_allocated": synthetic_cost,
                        "realized_pnl": q2(remaining_proceeds - synthetic_cost),
                        "cost_basis_status": synthetic_lot.cost_basis_status,
                        "pnl_status": "provisional",
                        "notes": "synthetic missing fund opening lot; true historical cost pending",
                    }
                )
                synthetic_lot.remaining_units = Decimal("0")
                synthetic_lot.remaining_cost = Decimal("0")
                self.add_validation(
                    check_code="synthetic_missing_fund_opening_lot",
                    status="skipped",
                    severity="warning",
                    instrument_key=event.fund.key,
                    source_table="fund_orders",
                    source_pk=event.order_id,
                    expected_value=event.units,
                    actual_value=q6(event.units - remaining_units),
                    diff_value=remaining_units,
                    message=f"{event.order_id} 缺少范围开始前基金 lot，已生成临时 opening fund lot；该笔收益为 provisional。",
                )

    def expected_latest_fund_units(self) -> tuple[str, dict[str, Decimal]]:
        latest_period = self.latest_period()
        expected: dict[str, Decimal] = {}
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category = 'fund'
              AND quantity IS NOT NULL
            """,
            (*params, latest_period),
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
                if remaining_excess <= FUND_DUST_UNITS_TOLERANCE:
                    self.add_validation(
                        check_code="fund_rounding_dust_units_carried",
                        status="skipped",
                        severity="warning",
                        instrument_key=fund_key,
                        expected_value=excess,
                        actual_value=q6(excess - remaining_excess),
                        diff_value=remaining_excess,
                        message=f"{fund_key} 剩余 {remaining_excess} 份基金尾差低于 dust 容忍阈值，保留为 rounding dust。",
                    )
                    continue
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
            if abs(diff) <= Q6:
                status = "passed"
                severity = "info"
                notes = None
            elif abs(diff) <= FUND_DUST_UNITS_TOLERANCE:
                status = "skipped"
                severity = "warning"
                notes = "fund rounding dust tolerated; no cash impact materiality inferred from raw amount"
            else:
                status = "failed"
                severity = "error"
                notes = None
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
                notes=notes,
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
                if is_closing_lot:
                    cost_allocated = q2(lot.remaining_cost)
                else:
                    cost_allocated = q2(lot.unit_cost * take_qty)
                if event.proceeds_policy == "cost_basis_transfer":
                    proceeds_allocated = cost_allocated
                elif is_last_piece_for_event:
                    proceeds_allocated = q2(remaining_proceeds)
                else:
                    proceeds_allocated = q2(event.proceeds * take_qty / event.quantity)
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
                        "pnl_status": event.pnl_status_override or ("provisional" if lot.cost_basis_status != "final" else "final"),
                        "notes": event.notes,
                    }
                )
                lot.remaining_quantity = q6(lot.remaining_quantity - take_qty)
                lot.remaining_cost = q2(lot.remaining_cost - cost_allocated)
                remaining_qty = q6(remaining_qty - take_qty)
                if event.proceeds_policy != "cost_basis_transfer":
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
                synthetic_cost = Decimal("0") if event.proceeds_policy == "cost_basis_transfer" else q2(remaining_proceeds)
                synthetic_proceeds = synthetic_cost if event.proceeds_policy == "cost_basis_transfer" else remaining_proceeds
                synthetic_lot = Lot(
                    lot_id=self.next_id("lot"),
                    account_id=self.account_id,
                    instrument_key=event.instrument_key,
                    instrument_code=event.instrument_code,
                    instrument_name=event.instrument_name,
                    market=None,
                    currency=event.currency,
                    source_type="synthetic_missing_opening_position",
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    source_ref=event.source_ref,
                    open_date=event.event_date,
                    settlement_date=event.settlement_date,
                    original_quantity=remaining_qty,
                    remaining_quantity=remaining_qty,
                    remaining_cost=synthetic_cost,
                    cost_basis_total=synthetic_cost,
                    cost_basis_principal=synthetic_cost,
                    cost_basis_fee=Decimal("0"),
                    cost_basis_status="provisional",
                    cost_basis_source="missing_pre_scope_open_lot_zero_pnl_placeholder",
                    notes="范围开始前缺少买入 / 持仓成本；临时按本次卖出 proceeds 生成占位 lot，使该段 realized PnL 为 0，等待更早结单或人工成本补录。",
                )
                self.add_lot(synthetic_lot)
                lots_by_key.setdefault(event.instrument_key, []).append(synthetic_lot)
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
                        "lot_id": synthetic_lot.lot_id,
                        "quantity_allocated": remaining_qty,
                        "proceeds_allocated": synthetic_proceeds,
                        "cost_allocated": synthetic_cost,
                        "realized_pnl": q2(synthetic_proceeds - synthetic_cost),
                        "cost_basis_status": synthetic_lot.cost_basis_status,
                        "pnl_status": event.pnl_status_override or "provisional",
                        "notes": f"synthetic missing opening lot; true historical cost pending; {event.notes or ''}".strip("; "),
                    }
                )
                synthetic_lot.remaining_quantity = Decimal("0")
                synthetic_lot.remaining_cost = Decimal("0")
                self.add_validation(
                    check_code="synthetic_missing_opening_lot",
                    status="skipped",
                    severity="warning",
                    instrument_key=event.instrument_key,
                    source_table=event.event_table,
                    source_pk=event.event_id,
                    expected_value=event.quantity,
                    actual_value=q6(event.quantity - remaining_qty),
                    diff_value=remaining_qty,
                    message=f"{event.event_id} 缺少范围开始前 lot，已生成临时 opening lot；该笔收益为 provisional。",
                )

    def latest_expected_long_position_keys(self) -> set[str]:
        latest_period = self.latest_period()
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND quantity > 0
            """,
            (*params, latest_period),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if inst.eligible_long_equity and inst.key:
                keys.add(inst.key)
        return keys

    def structured_product_disappearance_date(self, lot: Lot) -> tuple[str | None, str]:
        if not lot.instrument_code:
            return None, "structured product lot has no instrument code"
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT period, snapshot_type, code_name, market, currency, asset_category, quantity
            FROM position_snapshots
            WHERE {where}
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
              AND code_name LIKE ?
            ORDER BY period, snapshot_type
            """,
            (*params, f"%{lot.instrument_code}%"),
        ).fetchall()
        by_period: dict[str, dict[str, Decimal]] = {}
        last_positive_period: str | None = None
        for row in rows:
            inst = infer_instrument(
                code_raw=row["code_name"],
                market=row["market"],
                currency=row["currency"],
                instrument_type=row["asset_category"],
            )
            if inst.key != lot.instrument_key or inst.inferred_type != "hk_structured_product":
                continue
            snapshot_type = text(row["snapshot_type"]).lower()
            if snapshot_type in {"ending", "closing"}:
                bucket = "ending"
            elif snapshot_type == "opening":
                bucket = "opening"
            else:
                continue
            anchor_date = self.snapshot_anchor_date(text(row["period"]), bucket)
            if lot.open_date and anchor_date and anchor_date < lot.open_date:
                continue
            quantity = dec(row["quantity"])
            if quantity <= 0:
                continue
            period = text(row["period"])
            by_period.setdefault(period, {})
            by_period[period][bucket] = by_period[period].get(bucket, Decimal("0")) + quantity
            last_positive_period = period

        for period in sorted(by_period):
            opening_qty = by_period[period].get("opening")
            ending_qty = by_period[period].get("ending")
            if opening_qty and opening_qty > 0 and (ending_qty is None or ending_qty <= 0):
                return self.snapshot_anchor_date(period, "ending"), "opening_position_disappeared_before_period_end"

        if last_positive_period and last_positive_period < self.latest_period():
            return self.snapshot_anchor_date(last_positive_period, "ending"), "last_positive_statement_position_not_seen_later"
        return None, "no statement disappearance evidence"

    def expire_structured_product_residual_lots(self) -> None:
        latest_open_keys = self.latest_expected_long_position_keys()
        for lot in list(self.lots):
            if lot.remaining_quantity <= 0:
                continue
            if lot.instrument_key in latest_open_keys:
                continue
            if not looks_like_hk_structured_product(lot.instrument_name, lot.source_ref, lot.source_pk):
                continue
            expiry_date, evidence = self.structured_product_disappearance_date(lot)
            if not expiry_date:
                self.add_validation(
                    check_code="structured_product_expiry_unresolved",
                    status="skipped",
                    severity="warning",
                    instrument_key=lot.instrument_key,
                    source_table=lot.source_table,
                    source_pk=lot.source_pk,
                    actual_value=lot.remaining_quantity,
                    message=f"{lot.instrument_key} 疑似港股权证/牛熊证仍有剩余 lot，但缺少可定位的消失月份。",
                    notes=evidence,
                )
                continue

            quantity = lot.remaining_quantity
            cost_allocated = q2(lot.remaining_cost)
            allocation_id = self.next_id("allocation")
            self.allocations.append(
                {
                    "allocation_id": allocation_id,
                    "close_event_table": "synthetic_structured_product_expiry",
                    "close_event_id": f"{lot.lot_id}:{expiry_date}",
                    "close_event_date": expiry_date,
                    "close_settlement_date": None,
                    "close_source_ref": lot.source_ref,
                    "instrument_key": lot.instrument_key,
                    "instrument_code": lot.instrument_code,
                    "instrument_name": lot.instrument_name,
                    "currency": lot.currency,
                    "lot_id": lot.lot_id,
                    "quantity_allocated": quantity,
                    "proceeds_allocated": Decimal("0"),
                    "cost_allocated": cost_allocated,
                    "realized_pnl": -cost_allocated,
                    "cost_basis_status": lot.cost_basis_status,
                    "pnl_status": "provisional" if lot.cost_basis_status != "final" else "final",
                    "notes": f"港股权证/牛熊证在后续结单不再出现，按 {expiry_date} 到期/失效归零处理；evidence={evidence}。",
                }
            )
            lot.remaining_quantity = Decimal("0")
            lot.remaining_cost = Decimal("0")
            self.add_validation(
                check_code="structured_product_expiry_inferred",
                status="skipped",
                severity="warning",
                instrument_key=lot.instrument_key,
                source_table=lot.source_table,
                source_pk=lot.source_pk,
                expected_value=quantity,
                actual_value=Decimal("0"),
                diff_value=-quantity,
                message=f"{lot.instrument_key} 剩余权证/牛熊证 lot 已按结单消失月份归零。",
                notes=evidence,
            )

    def add_latest_position_deficit_lots(self) -> None:
        latest_period = self.latest_period()
        expected: dict[str, Decimal] = {}
        expected_names: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
            """,
            (*params, latest_period),
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
            expected_names[inst.key] = (inst.code, inst.name, inst.market, text(row["currency"]).upper())

        actual: dict[str, Decimal] = {}
        for lot in self.lots:
            actual[lot.instrument_key] = actual.get(lot.instrument_key, Decimal("0")) + lot.remaining_quantity

        for key, expected_qty in sorted(expected.items()):
            deficit = q6(expected_qty - actual.get(key, Decimal("0")))
            if deficit <= Q6:
                continue
            code, name, market, currency = expected_names[key]
            lot = Lot(
                lot_id=self.next_id("lot"),
                account_id=self.account_id,
                instrument_key=key,
                instrument_code=code,
                instrument_name=name,
                market=market,
                currency=currency or "HKD",
                source_type="synthetic_missing_position",
                source_table="position_snapshots",
                source_pk=f"{latest_period}:{key}",
                source_ref=None,
                open_date=period_start_date(latest_period),
                settlement_date=None,
                original_quantity=deficit,
                remaining_quantity=deficit,
                remaining_cost=Decimal("0"),
                cost_basis_total=Decimal("0"),
                cost_basis_principal=Decimal("0"),
                cost_basis_fee=Decimal("0"),
                cost_basis_status="provisional",
                cost_basis_source="latest_position_deficit_placeholder",
                notes="最新期末持仓多于可追溯 lot；通常来自账户升级/资产转入/拆股等历史成本缺口，需后续用更早结单或人工成本补录。",
            )
            self.add_lot(lot)
            self.add_validation(
                check_code="synthetic_missing_ending_position_lot",
                status="skipped",
                severity="warning",
                instrument_key=key,
                source_table="position_snapshots",
                source_pk=f"{latest_period}:{key}",
                expected_value=expected_qty,
                actual_value=q6(actual.get(key, Decimal("0"))),
                diff_value=deficit,
                message=f"{key} 最新期末持仓缺少可追溯 lot，已生成 provisional 持仓占位。",
            )

    def validate_remaining_positions(self) -> None:
        latest_period = self.latest_period()
        expected: dict[str, Decimal] = {}
        expected_names: dict[str, tuple[str | None, str | None, str | None]] = {}
        where, params = self.fact_where_clause()
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM position_snapshots
            WHERE {where}
              AND period = ?
              AND snapshot_type = 'ending'
              AND asset_category IN ('stock_or_option', 'stock_or_derivative')
              AND quantity IS NOT NULL
            """,
            (*params, latest_period),
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


def all_import_run_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT import_run_id FROM import_runs ORDER BY created_at, import_run_id",
        ).fetchall()
    ]


def selected_import_run_ids(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if getattr(args, "all_import_runs", False):
        run_ids = all_import_run_ids(conn)
    elif args.import_run_ids:
        run_ids = list(args.import_run_ids)
    else:
        run_ids = [latest_import_run_id(conn)]
    if not run_ids:
        raise RuntimeError("No import_runs found in database.")
    return run_ids


def statement_ids_for_account(conn: sqlite3.Connection, import_run_ids: list[str], account_id: str) -> list[str]:
    if not table_exists(conn, "statement_accounts"):
        return []
    placeholders = ", ".join("?" for _ in import_run_ids)
    return [
        row[0]
        for row in conn.execute(
            f"""
            SELECT statement_id
            FROM statement_accounts
            WHERE import_run_id IN ({placeholders})
              AND account_id = ?
            ORDER BY statement_id
            """,
            (*import_run_ids, account_id),
        ).fetchall()
    ]


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
        import_run_ids = selected_import_run_ids(conn, args)
        import_run_id = ",".join(import_run_ids)
        statement_ids = list(args.statement_ids or [])
        if not statement_ids:
            statement_ids = statement_ids_for_account(conn, import_run_ids, args.account_id)
        if args.replace:
            delete_existing_run(conn, allocation_run_id)
        conn.execute(
            """
            INSERT INTO lot_allocation_runs (
              allocation_run_id, import_run_id, account_id, method, scope, status, notes
            )
            VALUES (?, ?, ?, 'fifo', 'stock_ipo_option_fund_short_v1', 'running', ?)
            """,
            (
                allocation_run_id,
                import_run_id,
                args.account_id,
                "正股 / ETF long position + IPO 中签配发 + 期权合约 + 基金申赎 + 股票短仓 FIFO allocation v1。"
                + (f" statement_scope={len(statement_ids)}" if statement_ids else " statement_scope=all"),
            ),
        )
        allocator = LotAllocator(conn, allocation_run_id, import_run_ids, args.account_id, statement_ids=statement_ids)
        allocator.load_opening_lots()
        allocator.load_account_inception_lots_from_first_ending()
        allocator.load_first_observed_opening_lots()
        allocator.load_ipo_allotment_lots()
        allocator.load_asset_movement_lots()
        allocator.load_fund_lots_and_redemptions()
        allocator.load_market_trade_lots_and_closes()
        allocator.load_asset_movement_removals()
        allocator.load_option_lots_and_closes()
        allocator.load_option_exercise_events()
        allocator.allocate_fifo()
        allocator.expire_structured_product_residual_lots()
        allocator.allocate_options_fifo()
        allocator.allocate_funds_fifo()
        allocator.allocate_short_stock_fifo()
        allocator.mark_fund_pending_settlement_lots()
        allocator.add_latest_position_deficit_lots()
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
    run_parser.add_argument("--import-run-id", dest="import_run_ids", action="append", default=None)
    run_parser.add_argument("--all-import-runs", action="store_true")
    run_parser.add_argument("--account-id", default="futu_hk_main")
    run_parser.add_argument("--statement-id", dest="statement_ids", action="append", default=None)
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
