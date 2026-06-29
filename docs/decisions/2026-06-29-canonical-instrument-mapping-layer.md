# Decision: Canonical Instrument Mapping Layer

Date: 2026-06-29

## Background

Frontend and return-calculation design exposed a normalization gap: the same economic instrument can appear with different raw or platform-specific identifiers across fact tables.

Examples:

- Market trades can use broker symbols or market + code.
- Fund orders can use fund codes / ISIN-like identifiers.
- Corporate actions and cash rows can have incomplete raw descriptions.
- Option contracts have their own contract identity and also need an underlying instrument.
- Future brokers will introduce different contract ids, exchange codes, aliases, and naming conventions for the same instrument.

If the frontend reads raw / platform identifiers directly, instrument-level return tables cannot reliably aggregate or display the same instrument.

## Decision

Add a canonical instrument mapping layer:

`raw fact -> instrument resolution / master data -> lot/allocation -> return_items -> frontend`

Rules:

1. Raw fact tables keep broker raw code, raw name, and source text unchanged.
2. Parser may extract low-risk candidate identifiers, but must not overwrite source facts.
3. Instrument resolution maps platform instruments to `canonical_instrument_id`.
4. Lot / Allocation still runs at account + platform + platform instrument granularity.
5. Return calculation must emit `canonical_instrument_id`, `canonical_symbol`, and `canonical_display_name`.
6. Frontend defaults to canonical fields; raw fields are only for source trace and review.

## Proposed Schema

See `schema/canonical_instrument_mapping_schema_v1.sql`.

Core tables:

- `canonical_instruments`
- `platform_instrument_mappings`
- `instrument_resolution_queue`

Core views:

- `v_canonical_platform_instruments`
- `v_unresolved_instrument_candidates`

## Frontend Contract

The frontend should never build instrument names by concatenating raw code/name from facts. It should read enriched return rows with canonical fields.

Required fields in `return_items` or an enriched view:

| Field | Meaning |
| --- | --- |
| `instrument_key` | Platform/source instrument key. |
| `canonical_instrument_id` | Stable cross-platform instrument id. |
| `canonical_symbol` | Canonical display code. |
| `canonical_display_name` | Canonical display name. |
| `instrument_mapping_status` | `auto`, `manual_confirmed`, `needs_review`, `unmapped`. |

If an item cannot be resolved, use a stable temporary id such as `UNKNOWN:<hash>` and mark it `needs_review`.

