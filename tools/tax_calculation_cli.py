#!/usr/bin/env python3
"""Tax calculation trial CLI.

P0 scope: generate a RMB-denominated China IIT preparation estimate from the
latest realized return views and dividend-related cash facts. This is a
tax-preparation estimate, not filing advice.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "investment.sqlite"
DEFAULT_SCHEMA = WORKSPACE_ROOT / "schema" / "tax_calculation_schema_v1.sql"
DEFAULT_REPORT = WORKSPACE_ROOT / "exports" / "investment-db-v1" / "tax-cn-iit-2025-trial-report.md"

Q2 = Decimal("0.01")
DEFAULT_TAX_RATE = Decimal("0.20")


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value).replace(",", ""))


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_UP)


def dbnum(value: Decimal) -> str:
    return str(value)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def latest_allocation_run_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT allocation_run_id
        FROM lot_allocation_runs
        WHERE status IN ('passed', 'passed_with_warnings')
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No passed lot allocation run found.")
    return row[0]


@dataclass(frozen=True)
class FxRate:
    currency: str
    rate_to_cny: Decimal
    rate_date: str | None
    rate_type: str
    source_ref: str
    notes: str


def upsert_fx_rate_set(conn: sqlite3.Connection, *, rate_set_id: str, usd_cny: Decimal, hkd_cny: Decimal, notes: str) -> None:
    conn.execute(
        """
        INSERT INTO fx_rate_sets (
          rate_set_id, rate_set_name, base_currency, policy_id, rate_source, status, notes
        )
        VALUES (?, ?, 'CNY', ?, ?, 'active', ?)
        ON CONFLICT(rate_set_id) DO UPDATE SET
          rate_set_name = excluded.rate_set_name,
          policy_id = excluded.policy_id,
          rate_source = excluded.rate_source,
          status = excluded.status,
          notes = excluded.notes,
          updated_at = datetime('now')
        """,
        (
            rate_set_id,
            "CN IIT 2025 RMB trial FX rate set",
            "cn_iit_rmb_trial_estimate",
            "manual_seed_from_user_review; replace_with_pboC_safe_mid_rate_before_filing",
            notes,
        ),
    )
    rates = [
        FxRate("CNY", Decimal("1"), None, "base_currency", "manual", "人民币本位币。"),
        FxRate("USD", usd_cny, "2025", "annual_average_proxy", "manual_seed", "P0 试算用 USD/CNY。正式申报前应替换为中国税务口径人民币汇率中间价。"),
        FxRate("HKD", hkd_cny, "2025", "derived_annual_average_proxy", "USD_CNY / USD_HKD", "P0 试算用 HKD/CNY。正式申报前应替换为中国税务口径人民币汇率中间价。"),
    ]
    for rate in rates:
        conn.execute(
            """
            INSERT INTO fx_rates (
              rate_set_id, currency, rate_date, rate_to_cny, rate_type, source_ref, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rate_set_id, currency) DO UPDATE SET
              rate_date = excluded.rate_date,
              rate_to_cny = excluded.rate_to_cny,
              rate_type = excluded.rate_type,
              source_ref = excluded.source_ref,
              notes = excluded.notes,
              updated_at = datetime('now')
            """,
            (rate_set_id, rate.currency, rate.rate_date, dbnum(rate.rate_to_cny), rate.rate_type, rate.source_ref, rate.notes),
        )


def upsert_profile(conn: sqlite3.Connection, *, profile_id: str, tax_year: int, rate_set_id: str, tax_rate: Decimal, notes: str) -> None:
    conn.execute(
        """
        INSERT INTO tax_calculation_profiles (
          profile_id, profile_name, jurisdiction, tax_year, tax_rate, fx_rate_set_id,
          capital_gain_loss_policy, dividend_policy, withholding_credit_policy,
          financing_interest_policy, status, notes
        )
        VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(profile_id) DO UPDATE SET
          profile_name = excluded.profile_name,
          jurisdiction = excluded.jurisdiction,
          tax_year = excluded.tax_year,
          tax_rate = excluded.tax_rate,
          fx_rate_set_id = excluded.fx_rate_set_id,
          capital_gain_loss_policy = excluded.capital_gain_loss_policy,
          dividend_policy = excluded.dividend_policy,
          withholding_credit_policy = excluded.withholding_credit_policy,
          financing_interest_policy = excluded.financing_interest_policy,
          status = excluded.status,
          notes = excluded.notes,
          updated_at = datetime('now')
        """,
        (
            profile_id,
            "CN IIT 2025 trial estimate",
            tax_year,
            dbnum(tax_rate),
            rate_set_id,
            "net_final_realized_pnl_by_year",
            "cash_dividend_plus_other_related_income_before_credit",
            "recorded_withholding_tax_as_potential_credit_only_excluding_fee_like_labels",
            "excluded_by_default_review_required",
            notes,
        ),
    )


def fx_map(conn: sqlite3.Connection, rate_set_id: str) -> dict[str, Decimal]:
    rows = conn.execute("SELECT currency, rate_to_cny FROM fx_rates WHERE rate_set_id = ?", (rate_set_id,)).fetchall()
    rates = {row["currency"]: dec(row["rate_to_cny"]) for row in rows}
    missing = {"CNY", "HKD", "USD"} - set(rates)
    if missing:
        raise RuntimeError(f"Missing FX rates for {rate_set_id}: {sorted(missing)}")
    return rates


def amount_cny(amount: Decimal, currency: str, rates: dict[str, Decimal]) -> Decimal:
    return q2(amount * rates[currency])


def insert_item(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    item_type: str,
    item_subtype: str,
    source_table: str,
    source_pk: str,
    currency: str,
    source_amount: Decimal,
    rates: dict[str, Decimal],
    taxable_cny: Decimal,
    tax_rate: Decimal,
    creditable_tax_cny: Decimal = Decimal("0"),
    review_status: str = "auto_estimated",
    notes: str | None = None,
) -> None:
    cny = amount_cny(source_amount, currency, rates)
    tax_cny = q2(taxable_cny * tax_rate) if taxable_cny > 0 else Decimal("0")
    conn.execute(
        """
        INSERT INTO tax_calculation_items (
          tax_run_id, tax_item_id, item_type, item_subtype, source_table, source_pk,
          source_currency, source_amount, fx_rate_to_cny, amount_cny,
          taxable_cny, tax_rate, tax_cny, creditable_tax_cny, review_status, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            f"tax_item_{seq:04d}",
            item_type,
            item_subtype,
            source_table,
            source_pk,
            currency,
            dbnum(source_amount),
            dbnum(rates[currency]),
            dbnum(cny),
            dbnum(taxable_cny),
            dbnum(tax_rate),
            dbnum(tax_cny),
            dbnum(creditable_tax_cny),
            review_status,
            notes,
        ),
    )


def run_tax_calculation(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db_path.resolve()
    schema_path = args.schema_path.resolve()
    report_path = args.report_path.resolve()
    tax_year = int(args.tax_year)
    start_date = f"{tax_year}-01-01"
    end_date = f"{tax_year + 1}-01-01"
    tax_rate = dec(args.tax_rate)
    usd_cny = dec(args.usd_cny)
    hkd_cny = dec(args.hkd_cny)
    rate_set_id = args.rate_set_id or f"cn_iit_{tax_year}_rmb_trial_fx"
    profile_id = args.profile_id or f"cn_iit_{tax_year}_trial"
    run_id = args.run_id or f"tax_cn_iit_{tax_year}_trial_{utc_now_compact()}"

    notes = (
        "P0 试算：人民币税务准备口径；税率暂按 20%；汇率为手工种子估算。"
        "正式申报前需替换为主管税务机关认可的人民币汇率中间价，并复核亏损抵扣、费用扣除和境外税收抵免。"
    )

    with connect(db_path) as conn:
        apply_schema(conn, schema_path)
        source_allocation_run_id = args.allocation_run_id or latest_allocation_run_id(conn)
        with conn:
            upsert_fx_rate_set(conn, rate_set_id=rate_set_id, usd_cny=usd_cny, hkd_cny=hkd_cny, notes=notes)
            upsert_profile(conn, profile_id=profile_id, tax_year=tax_year, rate_set_id=rate_set_id, tax_rate=tax_rate, notes=notes)
            if args.replace:
                for table in (
                    "tax_calculation_validation_items",
                    "tax_calculation_summary_items",
                    "tax_calculation_items",
                    "tax_calculation_runs",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE tax_run_id = ?", (run_id,))
            conn.execute(
                """
                INSERT INTO tax_calculation_runs (
                  tax_run_id, profile_id, tax_year, source_allocation_run_id, status, notes
                )
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, profile_id, tax_year, source_allocation_run_id, notes),
            )
            rates = fx_map(conn, rate_set_id)
            seq = 1

            realized_rows = rows_as_dicts(
                conn,
                """
                SELECT pnl_layer, currency, ROUND(SUM(realized_pnl), 2) AS amount
                FROM v_realized_return_items
                WHERE allocation_run_id = ?
                  AND replace(event_date, '/', '-') >= ?
                  AND replace(event_date, '/', '-') < ?
                  AND pnl_status = 'final'
                GROUP BY pnl_layer, currency
                ORDER BY currency, pnl_layer
                """,
                (source_allocation_run_id, start_date, end_date),
            )
            for row in realized_rows:
                source_amount = q2(dec(row["amount"]))
                taxable = amount_cny(source_amount, row["currency"], rates)
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="realized_capital_gain",
                    item_subtype=row["pnl_layer"],
                    source_table="v_realized_return_items",
                    source_pk=f"{source_allocation_run_id}:{tax_year}:{row['pnl_layer']}:{row['currency']}",
                    currency=row["currency"],
                    source_amount=source_amount,
                    rates=rates,
                    taxable_cny=taxable,
                    tax_rate=tax_rate,
                    notes="final realized PnL netted within layer/currency for trial estimate.",
                )
                seq += 1

            dividend_rows = rows_as_dicts(
                conn,
                """
                SELECT currency,
                       ROUND(SUM(CASE WHEN cash_leg_type IN ('cash_dividend', 'other_corporate_action_cash')
                                      THEN amount ELSE 0 END), 2) AS dividend_income,
                       ROUND(SUM(CASE WHEN cash_leg_type = 'withholding_tax'
                                           AND lower(coalesce(description, '')) NOT LIKE '%handling fee%'
                                           AND lower(coalesce(description, '')) NOT LIKE '%scrip fee%'
                                           AND lower(coalesce(description, '')) NOT LIKE '%handling charge%'
                                           AND coalesce(description, '') NOT LIKE '%手续费%'
                                      THEN amount ELSE 0 END), 2) AS withholding_tax_credit,
                       ROUND(SUM(CASE WHEN cash_leg_type IN ('adr_fee', 'corporate_action_handling_charge')
                                      THEN amount
                                      WHEN cash_leg_type = 'withholding_tax'
                                           AND (
                                             lower(coalesce(description, '')) LIKE '%handling fee%'
                                             OR lower(coalesce(description, '')) LIKE '%scrip fee%'
                                             OR lower(coalesce(description, '')) LIKE '%handling charge%'
                                             OR coalesce(description, '') LIKE '%手续费%'
                                           )
                                      THEN amount ELSE 0 END), 2) AS dividend_related_fee
                FROM v_cash_ledger_entries_with_accounts
                WHERE replace(event_date, '/', '-') >= ?
                  AND replace(event_date, '/', '-') < ?
                  AND business_type = 'corporate_action'
                  AND cash_leg_type IN (
                    'cash_dividend',
                    'other_corporate_action_cash',
                    'withholding_tax',
                    'adr_fee',
                    'corporate_action_handling_charge'
                  )
                GROUP BY currency
                ORDER BY currency
                """,
                (start_date, end_date),
            )
            for row in dividend_rows:
                income = q2(dec(row["dividend_income"]))
                taxable = amount_cny(income, row["currency"], rates)
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="dividend_income",
                    item_subtype="cash_dividend_plus_related_income",
                    source_table="v_cash_ledger_entries_with_accounts",
                    source_pk=f"{tax_year}:corporate_action_dividend_income:{row['currency']}",
                    currency=row["currency"],
                    source_amount=income,
                    rates=rates,
                    taxable_cny=taxable,
                    tax_rate=tax_rate,
                    notes="cash_dividend + other_corporate_action_cash; withholding tax and fees are separate items.",
                )
                seq += 1

                withholding_abs = abs(q2(dec(row["withholding_tax_credit"])))
                credit = amount_cny(withholding_abs, row["currency"], rates)
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="foreign_tax_credit_candidate",
                    item_subtype="dividend_withholding_tax",
                    source_table="v_cash_ledger_entries_with_accounts",
                    source_pk=f"{tax_year}:corporate_action_withholding_tax:{row['currency']}",
                    currency=row["currency"],
                    source_amount=withholding_abs,
                    rates=rates,
                    taxable_cny=Decimal("0"),
                    tax_rate=tax_rate,
                    creditable_tax_cny=credit,
                    review_status="needs_review",
                    notes="记录为潜在境外税收抵免；明显手续费描述已排除，仍需确认税种、凭证和限额。",
                )
                seq += 1

                fee_abs = abs(q2(dec(row["dividend_related_fee"])))
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="dividend_related_fee",
                    item_subtype="adr_fee_or_handling_charge",
                    source_table="v_cash_ledger_entries_with_accounts",
                    source_pk=f"{tax_year}:corporate_action_dividend_fee:{row['currency']}",
                    currency=row["currency"],
                    source_amount=fee_abs,
                    rates=rates,
                    taxable_cny=Decimal("0"),
                    tax_rate=tax_rate,
                    review_status="needs_review",
                    notes="ADR fee / handling charge / fee-like withholding_tax 暂不自动抵扣税基，仅列示供复核。",
                )
                seq += 1

            stock_yield_rows = rows_as_dicts(
                conn,
                """
                SELECT currency, ROUND(SUM(cash_amount), 2) AS amount
                FROM stock_yield_cash_entries
                WHERE replace(event_date, '/', '-') >= ?
                  AND replace(event_date, '/', '-') < ?
                GROUP BY currency
                ORDER BY currency
                """,
                (start_date, end_date),
            )
            for row in stock_yield_rows:
                source_amount = q2(dec(row["amount"]))
                taxable = amount_cny(source_amount, row["currency"], rates)
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="stock_yield_income",
                    item_subtype="securities_lending_income",
                    source_table="stock_yield_cash_entries",
                    source_pk=f"{tax_year}:stock_yield:{row['currency']}",
                    currency=row["currency"],
                    source_amount=source_amount,
                    rates=rates,
                    taxable_cny=taxable,
                    tax_rate=tax_rate,
                    review_status="needs_review",
                    notes="股票出借收益暂按应税投资相关收入候选处理。",
                )
                seq += 1

            financing_rows = rows_as_dicts(
                conn,
                """
                SELECT currency, ROUND(SUM(amount), 2) AS amount
                FROM cash_ledger_entries
                WHERE replace(event_date, '/', '-') >= ?
                  AND replace(event_date, '/', '-') < ?
                  AND business_type = 'financing_interest'
                GROUP BY currency
                ORDER BY currency
                """,
                (start_date, end_date),
            )
            for row in financing_rows:
                source_amount = q2(dec(row["amount"]))
                insert_item(
                    conn,
                    run_id=run_id,
                    seq=seq,
                    item_type="financing_interest_expense",
                    item_subtype="deductibility_review_required",
                    source_table="cash_ledger_entries",
                    source_pk=f"{tax_year}:financing_interest:{row['currency']}",
                    currency=row["currency"],
                    source_amount=source_amount,
                    rates=rates,
                    taxable_cny=Decimal("0"),
                    tax_rate=tax_rate,
                    review_status="needs_review",
                    notes="融资利息默认不自动抵扣税基，单独列示。",
                )
                seq += 1

            taxable_income_cny = q2(dec(scalar(conn, "SELECT SUM(taxable_cny) FROM tax_calculation_items WHERE tax_run_id = ?", (run_id,))))
            tentative_tax_cny = q2(taxable_income_cny * tax_rate) if taxable_income_cny > 0 else Decimal("0")
            foreign_tax_credit_cny = q2(dec(scalar(conn, "SELECT SUM(creditable_tax_cny) FROM tax_calculation_items WHERE tax_run_id = ?", (run_id,))))
            estimated_tax_due_cny = q2(max(tentative_tax_cny - foreign_tax_credit_cny, Decimal("0")))

            summaries = [
                ("taxable_income_cny", None, None, taxable_income_cny, "试算应税收入人民币合计。"),
                ("tentative_tax_cny", None, None, tentative_tax_cny, "按 profile tax_rate 计算的初步税额。"),
                ("foreign_tax_credit_candidate_cny", None, None, foreign_tax_credit_cny, "潜在境外税收抵免候选。"),
                ("estimated_tax_due_cny", None, None, estimated_tax_due_cny, "初步税额减潜在抵免后的估算应补税额。"),
            ]
            for key, currency, amount, amount_cny_value, summary_notes in summaries:
                conn.execute(
                    """
                    INSERT INTO tax_calculation_summary_items (
                      tax_run_id, summary_key, currency, amount, amount_cny, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, key, currency, dbnum(amount) if amount is not None else None, dbnum(amount_cny_value), summary_notes),
                )

            for row in conn.execute(
                """
                SELECT item_type, source_currency, SUM(source_amount) AS amount, SUM(amount_cny) AS amount_cny
                FROM tax_calculation_items
                WHERE tax_run_id = ?
                GROUP BY item_type, source_currency
                ORDER BY item_type, source_currency
                """,
                (run_id,),
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO tax_calculation_summary_items (
                      tax_run_id, summary_key, currency, amount, amount_cny, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        f"source_{row['item_type']}",
                        row["source_currency"],
                        dbnum(q2(dec(row["amount"]))),
                        dbnum(q2(dec(row["amount_cny"]))),
                        "按 item_type / source_currency 汇总。",
                    ),
                )

            validations = [
                (
                    "fx_rate_policy_not_final",
                    "skipped",
                    "warning",
                    "本次使用手工种子 RMB 汇率试算，不是正式申报汇率。",
                    "正式申报前应替换为中国税务口径人民币汇率中间价。",
                ),
                (
                    "loss_offset_policy_review",
                    "skipped",
                    "warning",
                    "资本利得使用 final realized PnL 年度净额口径，亏损抵扣规则需税务复核。",
                    "如果不允许充分抵扣亏损，应税基会更高。",
                ),
                (
                    "withholding_credit_review",
                    "skipped",
                    "warning",
                    "预扣税仅作为潜在抵免候选，需复核凭证和限额；明显手续费描述不进入抵免候选。",
                    "港股已按净额入账的隐含预扣税未自动还原为抵免，长桥等平台的 Handling Fee / Scrip Fee 已列为费用待复核。",
                ),
                (
                    "financing_interest_excluded",
                    "skipped",
                    "warning",
                    "融资利息默认未抵扣税基。",
                    "如税务师确认可扣除，可用 profile override 重新计算。",
                ),
            ]
            for idx, (code, status, severity, message, validation_notes) in enumerate(validations, start=1):
                conn.execute(
                    """
                    INSERT INTO tax_calculation_validation_items (
                      tax_run_id, validation_item_id, check_code, status, severity, message, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, f"validation_{idx:04d}", code, status, severity, message, validation_notes),
                )

            conn.execute(
                """
                UPDATE tax_calculation_runs
                SET status = 'passed_with_warnings',
                    taxable_income_cny = ?,
                    tentative_tax_cny = ?,
                    foreign_tax_credit_cny = ?,
                    estimated_tax_due_cny = ?,
                    updated_at = datetime('now')
                WHERE tax_run_id = ?
                """,
                (
                    dbnum(taxable_income_cny),
                    dbnum(tentative_tax_cny),
                    dbnum(foreign_tax_credit_cny),
                    dbnum(estimated_tax_due_cny),
                    run_id,
                ),
            )

        report = build_report(conn, run_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")

        return {
            "status": "passed_with_warnings",
            "tax_run_id": run_id,
            "profile_id": profile_id,
            "tax_year": tax_year,
            "source_allocation_run_id": source_allocation_run_id,
            "fx_rate_set_id": rate_set_id,
            "report_path": str(report_path),
            "taxable_income_cny": str(taxable_income_cny),
            "tentative_tax_cny": str(tentative_tax_cny),
            "foreign_tax_credit_cny": str(foreign_tax_credit_cny),
            "estimated_tax_due_cny": str(estimated_tax_due_cny),
        }


def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_无数据_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def build_report(conn: sqlite3.Connection, run_id: str) -> str:
    run = rows_as_dicts(conn, "SELECT * FROM tax_calculation_runs WHERE tax_run_id = ?", (run_id,))[0]
    profile = rows_as_dicts(conn, "SELECT * FROM tax_calculation_profiles WHERE profile_id = ?", (run["profile_id"],))[0]
    rates = rows_as_dicts(conn, "SELECT currency, rate_to_cny, rate_type, notes FROM fx_rates WHERE rate_set_id = ? ORDER BY currency", (profile["fx_rate_set_id"],))
    summary = rows_as_dicts(
        conn,
        """
        SELECT summary_key, currency, amount, amount_cny, notes
        FROM tax_calculation_summary_items
        WHERE tax_run_id = ?
        ORDER BY summary_key, currency
        """,
        (run_id,),
    )
    items = rows_as_dicts(
        conn,
        """
        SELECT item_type, item_subtype, source_currency, source_amount, amount_cny,
               taxable_cny, tax_cny, creditable_tax_cny, review_status
        FROM tax_calculation_items
        WHERE tax_run_id = ?
        ORDER BY item_type, item_subtype, source_currency
        """,
        (run_id,),
    )
    validations = rows_as_dicts(
        conn,
        """
        SELECT check_code, status, severity, message, notes
        FROM tax_calculation_validation_items
        WHERE tax_run_id = ?
        ORDER BY validation_item_id
        """,
        (run_id,),
    )
    return "\n".join(
        [
            f"# CN IIT 税务试算报告：{run_id}",
            "",
            "## 结论",
            "",
            f"- tax_year: `{run['tax_year']}`",
            f"- profile: `{run['profile_id']}`",
            f"- source_allocation_run_id: `{run['source_allocation_run_id']}`",
            f"- taxable_income_cny: `{run['taxable_income_cny']}`",
            f"- tentative_tax_cny: `{run['tentative_tax_cny']}`",
            f"- foreign_tax_credit_cny: `{run['foreign_tax_credit_cny']}`",
            f"- estimated_tax_due_cny: `{run['estimated_tax_due_cny']}`",
            "",
            "## 汇率",
            "",
            md_table(rates, ["currency", "rate_to_cny", "rate_type", "notes"]),
            "",
            "## 明细",
            "",
            md_table(
                items,
                [
                    "item_type",
                    "item_subtype",
                    "source_currency",
                    "source_amount",
                    "amount_cny",
                    "taxable_cny",
                    "tax_cny",
                    "creditable_tax_cny",
                    "review_status",
                ],
            ),
            "",
            "## 汇总",
            "",
            md_table(summary, ["summary_key", "currency", "amount", "amount_cny", "notes"]),
            "",
            "## 校验 / 待复核",
            "",
            md_table(validations, ["check_code", "status", "severity", "message", "notes"]),
            "",
        ]
    )


def status(args: argparse.Namespace) -> dict[str, Any]:
    with connect(args.db_path.resolve()) as conn:
        rows = rows_as_dicts(
            conn,
            """
            SELECT tax_run_id, profile_id, tax_year, status, taxable_income_cny,
                   tentative_tax_cny, foreign_tax_credit_cny, estimated_tax_due_cny,
                   created_at
            FROM tax_calculation_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        )
    return {"runs": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    run.add_argument("--schema-path", type=Path, default=DEFAULT_SCHEMA)
    run.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    run.add_argument("--tax-year", type=int, default=2025)
    run.add_argument("--allocation-run-id")
    run.add_argument("--run-id")
    run.add_argument("--profile-id")
    run.add_argument("--rate-set-id")
    run.add_argument("--tax-rate", default="0.20")
    run.add_argument("--usd-cny", default="7.129")
    run.add_argument("--hkd-cny", default="0.9144433042585941508465879938")
    run.add_argument("--replace", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    st.add_argument("--limit", type=int, default=10)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        result = run_tax_calculation(args)
    elif args.command == "status":
        result = status(args)
    else:
        raise RuntimeError(f"Unhandled command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
