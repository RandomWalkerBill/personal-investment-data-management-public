#!/usr/bin/env python3
"""Repository smoke test.

This test does not require private statements or a real investment database.
It checks that Python tools compile and that SQL schemas can be applied to an
empty SQLite database in the expected order.
"""

from __future__ import annotations

import py_compile
import sqlite3
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TOOLS = [
    "futu_statement_parser_v1.py",
    "futu_ingest_cli.py",
    "futu_annual_bill_ingest_cli.py",
    "investment_db_cli.py",
    "investment_import_service_cli.py",
    "canonical_instrument_mapping_cli.py",
    "investment_continuity_check.py",
    "lot_allocation_cli.py",
    "ipo_report_cli.py",
    "tax_calculation_cli.py",
]
SCHEMAS = [
    "futu_raw_fact_schema_v1.sql",
    "investment_management_schema_v1.sql",
    "canonical_account_mapping_schema_v1.sql",
    "canonical_instrument_mapping_schema_v1.sql",
    "import_service_schema_v1.sql",
    "lot_allocation_schema_v1.sql",
    "tax_calculation_schema_v1.sql",
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pycache_dir = tmp_path / "pycache"
        pycache_dir.mkdir(parents=True, exist_ok=True)
        for tool in TOOLS:
            py_compile.compile(
                str(ROOT / "tools" / tool),
                cfile=str(pycache_dir / f"{tool}.pyc"),
                doraise=True,
            )

        db_path = tmp_path / "smoke.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            for schema in SCHEMAS:
                conn.executescript((ROOT / "schema" / schema).read_text(encoding="utf-8"))
        finally:
            conn.close()

    print("smoke test passed")


if __name__ == "__main__":
    main()
