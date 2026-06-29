#!/usr/bin/env python3
"""Build canonical instrument mappings from the current investment database."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "canonical_instrument_mapping_schema_v1.sql"

HK_CODE_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")
FUND_CODE_RE = re.compile(r"(HK\d{10})")
OPTION_CODE_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)$")
OPTION_CODE_ANY_RE = re.compile(r"([A-Z]+\d{6}[CP]\d+)")
NOISY_NAME_MARKERS = ("保證金", "综合帳戶", "綜合帳戶", "10:", " 10:")


@dataclass(frozen=True)
class Candidate:
    source_table: str
    source_pk: str
    platform_key: str
    raw_symbol: str | None
    raw_name: str | None
    raw_text: str | None
    instrument_type: str
    canonical_id: str
    canonical_symbol: str
    canonical_name: str | None
    canonical_type: str
    primary_market: str | None
    listing_currency: str | None
    confidence: str
    status: str = "auto"
    notes: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sqlite_object_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    return row is not None


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value).replace("\n", " ")).strip()


def clean_name(raw_name: str | None, raw_symbol: str | None = None) -> str | None:
    name = normalize_text(raw_name)
    if not name:
        return None
    if name.startswith("IPO Allotment Qty"):
        return None
    leading_hk = re.match(r"^\d{4,5}\(([^)]+)\)", name)
    if leading_hk:
        return leading_hk.group(1).strip() or None
    if raw_symbol and name == raw_symbol:
        return None
    if "(" in name and ")" in name:
        match = re.match(r"^[A-Z0-9.]+[\s]*\((.+)\)$", name)
        if match:
            name = match.group(1).strip()
    if ")" in name and "(" not in name:
        name = name.split(")", 1)[0].strip()
    for marker in NOISY_NAME_MARKERS:
        if marker in name:
            name = name.split(marker, 1)[0].strip()
    if raw_symbol:
        name = re.sub(rf"^{re.escape(raw_symbol)}\s*", "", name).strip()
    return name or None


def name_score(name: str | None, source_table: str) -> int:
    if not name:
        return 0
    score = min(len(name), 40)
    if source_table in {"position_lots", "fund_position_lots", "short_stock_lots", "option_contract_lots"}:
        score += 40
    if source_table == "market_trades":
        score += 25
    if any(marker in name for marker in NOISY_NAME_MARKERS):
        score -= 80
    if re.fullmatch(r"[A-Z0-9.() -]+", name):
        score -= 10
    return score


def hk_code_from_text(*values: str | None) -> str | None:
    for value in values:
        text = normalize_text(value)
        match = HK_CODE_RE.search(text)
        if match:
            return match.group(1).zfill(5)
    return None


def fund_code_from_text(*values: str | None) -> str | None:
    for value in values:
        text = normalize_text(value)
        match = FUND_CODE_RE.search(text)
        if match:
            return match.group(1)
    return None


def parse_option_code(option_code: str) -> tuple[str, str, str, str] | None:
    match = OPTION_CODE_RE.match(option_code or "")
    if not match:
        return None
    underlying, yy, mm, dd, cp, strike_raw = match.groups()
    year = int(yy)
    year += 2000 if year < 70 else 1900
    strike_value = int(strike_raw) / 1000
    strike = f"{strike_value:.3f}".rstrip("0").rstrip(".")
    return underlying, f"{year:04d}-{mm}-{dd}", cp, strike


def option_code_from_text(*values: str | None) -> str | None:
    for value in values:
        text = normalize_text(value)
        match = OPTION_CODE_ANY_RE.search(text)
        if match:
            return match.group(1)
    return None


def canonical_for_stock(
    key: str | None,
    code: str | None,
    raw_name: str | None,
    market: str | None,
    currency: str | None,
    source_table: str,
) -> Candidate | None:
    key_text = normalize_text(key)
    code_text = normalize_text(code)
    market_text = normalize_text(market).upper()
    currency_text = normalize_text(currency).upper()
    name_source = raw_name or code_text or key_text
    hk_code = hk_code_from_text(key_text, code_text, raw_name)
    if hk_code and (key_text.startswith("HK:") or market_text in {"SEHK", "HKEX", "HK"} or currency_text == "HKD"):
        platform_key = f"HK:{hk_code}"
        return Candidate(
            source_table=source_table,
            source_pk=platform_key,
            platform_key=platform_key,
            raw_symbol=hk_code,
            raw_name=clean_name(name_source, hk_code),
            raw_text=name_source,
            instrument_type="stock_or_etf",
            canonical_id=f"HKEX:{hk_code}",
            canonical_symbol=f"HK:{hk_code}",
            canonical_name=clean_name(name_source, hk_code),
            canonical_type="stock_or_etf",
            primary_market="HKEX",
            listing_currency="HKD",
            confidence="high",
        )
    symbol = code_text or key_text.replace("US:", "")
    if symbol and (key_text.startswith("US:") or market_text in {"US", "NASDAQ", "NYSE", "AMEX", "ARCA", "BATO", "JNST", "EDGX", "EDGO", "MCRY"} or currency_text == "USD"):
        symbol = symbol.split("(", 1)[0].strip().upper()
        if re.fullmatch(r"[A-Z.]{1,8}", symbol):
            platform_key = f"US:{symbol}"
            name_source = raw_name or code_text or key_text
            return Candidate(
                source_table=source_table,
                source_pk=platform_key,
                platform_key=platform_key,
                raw_symbol=symbol,
                raw_name=clean_name(name_source, symbol),
                raw_text=name_source,
                instrument_type="stock_or_etf",
                canonical_id=f"US:{symbol}",
                canonical_symbol=symbol,
                canonical_name=clean_name(name_source, symbol),
                canonical_type="stock_or_etf",
                primary_market="US",
                listing_currency="USD",
                confidence="medium" if market_text not in {"US", "NASDAQ", "NYSE", "AMEX", "ARCA"} else "high",
            )
    return None


def canonical_for_fund(code: str | None, raw_name: str | None, currency: str | None, source_table: str) -> Candidate | None:
    fund_code = fund_code_from_text(code, raw_name)
    if not fund_code:
        return None
    platform_key = f"FUND:{fund_code}"
    return Candidate(
        source_table=source_table,
        source_pk=platform_key,
        platform_key=platform_key,
        raw_symbol=fund_code,
        raw_name=clean_name(raw_name, fund_code),
        raw_text=raw_name or code or fund_code,
        instrument_type="fund",
        canonical_id=f"FUND:{fund_code}",
        canonical_symbol=fund_code,
        canonical_name=clean_name(raw_name, fund_code),
        canonical_type="fund",
        primary_market=None,
        listing_currency=normalize_text(currency).upper() or None,
        confidence="high",
    )


def canonical_for_option(
    option_code: str | None,
    option_key: str | None,
    raw_name: str | None,
    currency: str | None,
    source_table: str,
) -> Candidate | None:
    code = option_code_from_text(option_code) or normalize_text(option_code)
    if not code:
        return None
    platform_key = f"OPTION:{code}"
    parsed = parse_option_code(code)
    if parsed:
        underlying, expiry, cp, strike = parsed
        canonical_id = f"OPT:{underlying}:{expiry}:{cp}:{strike}"
        canonical_symbol = f"{underlying} {expiry} {cp} {strike}"
    else:
        canonical_id = normalize_text(option_key) or f"OPTION:{code}"
        canonical_symbol = code
    return Candidate(
        source_table=source_table,
        source_pk=platform_key,
        platform_key=platform_key,
        raw_symbol=code,
        raw_name=clean_name(raw_name, code),
        raw_text=raw_name or code,
        instrument_type="option",
        canonical_id=canonical_id,
        canonical_symbol=canonical_symbol,
        canonical_name=None if (raw_name or "").startswith("OPT:") else (clean_name(raw_name, code) or canonical_symbol),
        canonical_type="option",
        primary_market=None,
        listing_currency=normalize_text(currency).upper() or None,
        confidence="high" if parsed else "medium",
    )


def unknown_candidate(source_table: str, source_pk: str, raw_text: str | None, instrument_type: str) -> Candidate:
    canonical_id = "UNKNOWN:" + hashlib.sha1((raw_text or source_pk).encode("utf-8")).hexdigest()[:12]
    return Candidate(
        source_table=source_table,
        source_pk=source_pk,
        platform_key=f"UNKNOWN:{hashlib.sha1(source_pk.encode('utf-8')).hexdigest()[:12]}",
        raw_symbol=None,
        raw_name=clean_name(raw_text),
        raw_text=raw_text,
        instrument_type=instrument_type or "unknown",
        canonical_id=canonical_id,
        canonical_symbol=canonical_id,
        canonical_name=clean_name(raw_text),
        canonical_type="unknown",
        primary_market=None,
        listing_currency=None,
        confidence="low",
        status="needs_review",
        notes="auto-generated unknown canonical instrument; requires review",
    )


def collect_candidates(conn: sqlite3.Connection) -> list[Candidate]:
    candidates: list[Candidate] = []

    if sqlite_object_exists(conn, "position_lots"):
        for row in conn.execute(
            """
            SELECT DISTINCT instrument_key, instrument_code, instrument_name, currency
            FROM position_lots
            WHERE instrument_key IS NOT NULL
            """
        ):
            candidate = canonical_for_stock(
                row["instrument_key"], row["instrument_code"], row["instrument_name"], None, row["currency"], "position_lots"
            )
            if candidate:
                candidates.append(candidate)

    if sqlite_object_exists(conn, "short_stock_lots"):
        for row in conn.execute(
            """
            SELECT DISTINCT instrument_key, instrument_code, instrument_name, currency
            FROM short_stock_lots
            WHERE instrument_key IS NOT NULL
            """
        ):
            candidate = canonical_for_stock(
                row["instrument_key"], row["instrument_code"], row["instrument_name"], None, row["currency"], "short_stock_lots"
            )
            if candidate:
                candidates.append(candidate)

    if sqlite_object_exists(conn, "fund_position_lots"):
        for row in conn.execute(
            """
            SELECT DISTINCT fund_key, fund_code, fund_name, currency
            FROM fund_position_lots
            WHERE fund_code IS NOT NULL
            """
        ):
            candidate = canonical_for_fund(row["fund_code"], row["fund_name"], row["currency"], "fund_position_lots")
            if candidate:
                candidates.append(candidate)

    if sqlite_object_exists(conn, "option_contract_lots"):
        for row in conn.execute(
            """
            SELECT DISTINCT option_code, option_contract_key, currency
            FROM option_contract_lots
            WHERE option_code IS NOT NULL
            """
        ):
            candidate = canonical_for_option(
                row["option_code"], row["option_contract_key"], row["option_contract_key"], row["currency"], "option_contract_lots"
            )
            if candidate:
                candidates.append(candidate)

    if sqlite_object_exists(conn, "v_instrument_candidates"):
        for row in conn.execute(
            """
            SELECT market, instrument_code, display_name, instrument_type, currency, source_table
            FROM v_instrument_candidates
            WHERE instrument_code IS NOT NULL
            """
        ):
            source_table = row["source_table"] or "v_instrument_candidates"
            instrument_type = row["instrument_type"] or "unknown"
            option_code = option_code_from_text(row["instrument_code"], row["display_name"])
            if option_code:
                candidate = canonical_for_option(option_code, None, row["display_name"], row["currency"], source_table)
            elif instrument_type == "fund":
                candidate = canonical_for_fund(row["instrument_code"], row["display_name"], row["currency"], source_table)
            else:
                candidate = canonical_for_stock(
                    row["instrument_code"], row["instrument_code"], row["display_name"], row["market"], row["currency"], source_table
                )
            if candidate:
                candidates.append(candidate)
            else:
                candidates.append(
                    unknown_candidate(
                        source_table,
                        f"{source_table}:{row['instrument_code']}",
                        row["display_name"] or row["instrument_code"],
                        instrument_type,
                    )
                )

    candidates.extend(system_pseudo_instruments())
    return candidates


def system_pseudo_instruments() -> list[Candidate]:
    rows = [
        ("CASH:HKD", "HKD Cash", "cash", "HKD"),
        ("CASH:USD", "USD Cash", "cash", "USD"),
        ("CASH:CNY", "CNY Cash", "cash", "CNY"),
        ("EXPENSE:FINANCING_INTEREST", "Financing interest", "period_expense", None),
        ("INCOME:BROKER_REWARD", "Broker reward", "other_income", None),
        ("INCOME:CORPORATE_ACTION", "Corporate action income", "investment_income", None),
        ("INCOME:STOCK_YIELD", "Stock yield income", "investment_income", None),
        ("FLOW:EXTERNAL_TRANSFER", "External transfer", "external_flow", None),
    ]
    candidates: list[Candidate] = []
    for canonical_id, name, instrument_type, currency in rows:
        candidates.append(
            Candidate(
                source_table="system",
                source_pk=canonical_id,
                platform_key=canonical_id,
                raw_symbol=canonical_id,
                raw_name=name,
                raw_text=name,
                instrument_type=instrument_type,
                canonical_id=canonical_id,
                canonical_symbol=canonical_id,
                canonical_name=name,
                canonical_type=instrument_type,
                primary_market=None,
                listing_currency=currency,
                confidence="high",
            )
        )
    return candidates


def pick_best_by_canonical(candidates: list[Candidate]) -> dict[str, Candidate]:
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.canonical_id)
        if current is None:
            best[candidate.canonical_id] = candidate
            continue
        if name_score(candidate.canonical_name, candidate.source_table) > name_score(current.canonical_name, current.source_table):
            best[candidate.canonical_id] = candidate
    return best


def pick_best_by_platform_key(candidates: list[Candidate]) -> dict[str, Candidate]:
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.platform_key)
        if current is None:
            best[candidate.platform_key] = candidate
            continue
        if candidate.confidence == "high" and current.confidence != "high":
            best[candidate.platform_key] = candidate
        elif name_score(candidate.canonical_name, candidate.source_table) > name_score(current.canonical_name, current.source_table):
            best[candidate.platform_key] = candidate
    return best


def seed_mappings(conn: sqlite3.Connection, reset: bool, source_scope: str) -> dict[str, Any]:
    candidates = collect_candidates(conn)
    best_canonical = pick_best_by_canonical(candidates)
    best_platform = pick_best_by_platform_key(candidates)
    mapping_run_id = f"canonical_mapping_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with conn:
        if reset:
            conn.execute("DELETE FROM instrument_resolution_queue")
            conn.execute("DELETE FROM platform_instrument_mappings")
            conn.execute("DELETE FROM canonical_instruments")

        conn.execute(
            """
            INSERT OR REPLACE INTO canonical_instrument_mapping_runs (
              mapping_run_id, source_scope, status, candidate_count, notes
            ) VALUES (?, ?, 'running', ?, ?)
            """,
            (mapping_run_id, source_scope, len(candidates), "seeded from current raw facts and lot/allocation tables"),
        )

        for canonical_id, candidate in sorted(best_canonical.items()):
            conn.execute(
                """
                INSERT INTO canonical_instruments (
                  canonical_instrument_id, canonical_symbol, canonical_name, instrument_type,
                  primary_market, listing_currency, status, review_status, source, updated_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_instrument_id) DO UPDATE SET
                  canonical_symbol = excluded.canonical_symbol,
                  canonical_name = COALESCE(excluded.canonical_name, canonical_instruments.canonical_name),
                  instrument_type = excluded.instrument_type,
                  primary_market = COALESCE(excluded.primary_market, canonical_instruments.primary_market),
                  listing_currency = COALESCE(excluded.listing_currency, canonical_instruments.listing_currency),
                  review_status = CASE
                    WHEN canonical_instruments.review_status = 'manual_confirmed' THEN canonical_instruments.review_status
                    ELSE excluded.review_status
                  END,
                  updated_at = excluded.updated_at,
                  notes = COALESCE(excluded.notes, canonical_instruments.notes)
                """,
                (
                    canonical_id,
                    candidate.canonical_symbol,
                    candidate.canonical_name,
                    candidate.canonical_type,
                    candidate.primary_market,
                    candidate.listing_currency,
                    "needs_review" if candidate.status == "needs_review" else "active",
                    "needs_review" if candidate.status == "needs_review" else "auto_resolved",
                    "derived_from_private_facts" if candidate.source_table != "system" else "system",
                    utc_now(),
                    candidate.notes,
                ),
            )

        for platform_key, candidate in sorted(best_platform.items()):
            conn.execute(
                """
                INSERT INTO platform_instrument_mappings (
                  mapping_id, platform_id, account_id, platform_instrument_key,
                  raw_instrument_text, raw_symbol, raw_name, instrument_type,
                  canonical_instrument_id, mapping_confidence, mapping_status,
                  source_refs, updated_at, notes
                ) VALUES (?, 'futu', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mapping_id) DO UPDATE SET
                  raw_instrument_text = excluded.raw_instrument_text,
                  raw_symbol = excluded.raw_symbol,
                  raw_name = COALESCE(excluded.raw_name, platform_instrument_mappings.raw_name),
                  instrument_type = excluded.instrument_type,
                  canonical_instrument_id = excluded.canonical_instrument_id,
                  mapping_confidence = excluded.mapping_confidence,
                  mapping_status = CASE
                    WHEN platform_instrument_mappings.mapping_status = 'manual_confirmed' THEN platform_instrument_mappings.mapping_status
                    ELSE excluded.mapping_status
                  END,
                  source_refs = excluded.source_refs,
                  updated_at = excluded.updated_at,
                  notes = COALESCE(excluded.notes, platform_instrument_mappings.notes)
                """,
                (
                    stable_id("mapping", "futu", platform_key),
                    platform_key,
                    candidate.raw_text,
                    candidate.raw_symbol,
                    candidate.raw_name,
                    candidate.instrument_type,
                    candidate.canonical_id,
                    candidate.confidence,
                    candidate.status,
                    candidate.source_table,
                    utc_now(),
                    candidate.notes,
                ),
            )

        unresolved = [candidate for candidate in best_platform.values() if candidate.status == "needs_review"]
        for candidate in unresolved:
            conn.execute(
                """
                INSERT OR REPLACE INTO instrument_resolution_queue (
                  queue_id, source_table, source_pk, platform_id, account_id, platform_instrument_key,
                  raw_instrument_text, raw_symbol, raw_name, instrument_type,
                  suggested_canonical_instrument_id, reason, status, notes
                ) VALUES (?, ?, ?, 'futu', NULL, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    stable_id("queue", candidate.source_table, candidate.source_pk, candidate.platform_key),
                    candidate.source_table,
                    candidate.source_pk,
                    candidate.platform_key,
                    candidate.raw_text,
                    candidate.raw_symbol,
                    candidate.raw_name,
                    candidate.instrument_type,
                    candidate.canonical_id,
                    "unresolved_or_low_confidence_instrument_mapping",
                    candidate.notes,
                ),
            )

        canonical_count = conn.execute("SELECT COUNT(*) AS c FROM canonical_instruments").fetchone()["c"]
        mapping_count = conn.execute("SELECT COUNT(*) AS c FROM platform_instrument_mappings").fetchone()["c"]
        unresolved_count = conn.execute("SELECT COUNT(*) AS c FROM v_unresolved_instrument_candidates").fetchone()["c"]
        status = "needs_review" if unresolved_count else "passed"
        conn.execute(
            """
            UPDATE canonical_instrument_mapping_runs
            SET status = ?, canonical_count = ?, mapping_count = ?, unresolved_count = ?
            WHERE mapping_run_id = ?
            """,
            (status, canonical_count, mapping_count, unresolved_count, mapping_run_id),
        )

    return {
        "mapping_run_id": mapping_run_id,
        "status": status,
        "candidate_count": len(candidates),
        "canonical_count": canonical_count,
        "mapping_count": mapping_count,
        "unresolved_count": unresolved_count,
    }


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {}
    for table in ["canonical_instruments", "platform_instrument_mappings", "v_unresolved_instrument_candidates"]:
        if sqlite_object_exists(conn, table):
            tables[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    coverage = []
    if sqlite_object_exists(conn, "v_canonical_instrument_resolution_coverage"):
        coverage = [dict(row) for row in conn.execute("SELECT * FROM v_canonical_instrument_resolution_coverage")]
    latest_run = None
    if sqlite_object_exists(conn, "canonical_instrument_mapping_runs"):
        row = conn.execute(
            """
            SELECT * FROM canonical_instrument_mapping_runs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_run = dict(row) if row else None
    return {"tables": tables, "coverage": coverage, "latest_run": latest_run}


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical instrument mapping CLI")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-path", type=Path, default=DEFAULT_SCHEMA)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Apply schema and seed canonical mappings")
    run_parser.add_argument("--reset", action="store_true", help="Delete generated canonical mapping rows before seeding")
    run_parser.add_argument("--source-scope", default="current_investment_db")

    subparsers.add_parser("status", help="Show canonical mapping status")
    subparsers.add_parser("apply-schema", help="Apply canonical mapping schema only")

    args = parser.parse_args()
    conn = connect(args.db_path)
    try:
        if args.command == "apply-schema":
            apply_schema(conn, args.schema_path)
            conn.commit()
            print(json.dumps({"status": "schema_applied", "db_path": str(args.db_path)}, ensure_ascii=False, indent=2))
        elif args.command == "run":
            apply_schema(conn, args.schema_path)
            result = seed_mappings(conn, reset=args.reset, source_scope=args.source_scope)
            result["db_path"] = str(args.db_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(status(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
