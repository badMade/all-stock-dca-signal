"""No account-shaped literal can be stored: accounts and tax_lots hold aliases only.

Proposed default (ADR open question 3, NOT operator-approved): alias = salted SHA-256
(HMAC-SHA256) of the raw account identifier, salt supplied at runtime through
``$DCA_LEDGER_ACCOUNT_SALT`` and never stored in the repo or the database. The raw
identifier is converted by ``account_alias()`` BEFORE a row is built; the ingest then
refuses any row that still carries a raw identifier, under any key or inside any text.

No account-shaped literal appears in this file: the raw identifiers below are built at
runtime from repeated digits so tests/test_config_invariants.py has nothing to flag.
"""

from __future__ import annotations

from datetime import date

import pytest

from .conftest import FIRST_SEEN, TEST_SALT, utc

RAW_A = "4" * 9  # synthetic, built at runtime; nine digits like a real account number
RAW_B = "7" * 8


def _lot(alias, **extra):
    row = {
        "account_alias": alias,
        "symbol": "ZZZA",
        "lot_id": "lot-aaaa-bbbb",
        "acquired_date": date(2026, 3, 2),
        "snapshot_date": date(2026, 4, 1),
        "quantity": 2.5,
        "cost_basis_per_share": 10.0,
    }
    row.update(extra)
    return row


def test_alias_is_deterministic_salted_and_hides_the_raw_identifier(salt):
    from market_data_ingest.ledger.accounts import ALIAS_PATTERN, account_alias

    a1 = account_alias(RAW_A)
    assert a1 == account_alias(RAW_A)  # deterministic, so lots join across snapshots
    assert ALIAS_PATTERN.fullmatch(a1)
    assert RAW_A not in a1
    assert account_alias(RAW_A, salt=TEST_SALT + "-other") != a1  # salt matters
    assert account_alias(RAW_B) != a1


def test_alias_requires_a_salt_from_the_environment(monkeypatch):
    from market_data_ingest.ledger import defaults
    from market_data_ingest.ledger.accounts import account_alias
    from market_data_ingest.ledger.errors import AccountIdentifierError

    assert defaults.ACCOUNT_SALT_ENV == "DCA_LEDGER_ACCOUNT_SALT"
    monkeypatch.delenv("DCA_LEDGER_ACCOUNT_SALT", raising=False)
    with pytest.raises(AccountIdentifierError, match="DCA_LEDGER_ACCOUNT_SALT"):
        account_alias(RAW_A)
    with pytest.raises(AccountIdentifierError):
        account_alias(RAW_A, salt="short")  # below MIN_SALT_LENGTH


def test_error_messages_never_echo_the_raw_identifier(ledger, salt):
    from market_data_ingest.ledger.errors import AccountIdentifierError

    with pytest.raises(AccountIdentifierError) as exc:
        ledger.ingest_accounts([{"account_alias": RAW_A, "account_kind": "agentic"}], source="t", run_id="r",
                               as_of=FIRST_SEEN)
    assert RAW_A not in str(exc.value)


@pytest.mark.parametrize(
    "row",
    [
        {"account_alias": RAW_A, "account_kind": "agentic"},  # raw digits where an alias belongs
        {"account_alias": "not-an-alias", "account_kind": "agentic"},  # wrong alias shape
        {"account_number": RAW_A, "account_kind": "agentic"},  # raw identifier under its own key
        {"account_alias": None, "account_kind": "agentic", "label": f"robinhood-{RAW_A}"},
    ],
    ids=["raw_digits_as_alias", "bad_alias_shape", "raw_key", "missing_alias_labelled_text"],
)
def test_accounts_ingest_rejects_raw_identifiers(ledger, salt, row):
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError):
        ledger.ingest_accounts([row], source="t", run_id="r", as_of=FIRST_SEEN)
    assert ledger.row_count("accounts") == 0


def test_label_with_a_labelled_account_number_is_rejected(ledger, salt):
    from market_data_ingest.ledger.accounts import account_alias
    from market_data_ingest.ledger.errors import AccountIdentifierError

    row = {"account_alias": account_alias(RAW_A), "account_kind": "agentic", "label": f"acct {RAW_B}"}
    with pytest.raises(AccountIdentifierError):
        ledger.ingest_accounts([row], source="t", run_id="r", as_of=FIRST_SEEN)


def test_accounts_store_only_the_alias(ledger, salt, db_path):
    from market_data_ingest.ledger.accounts import account_alias

    alias = account_alias(RAW_A)
    rows = [{"account_alias": alias, "account_kind": "agentic", "label": "agentic"}]
    assert ledger.ingest_accounts(rows, source="broker_accounts", run_id="r", as_of=FIRST_SEEN) == 1
    assert ledger.accounts() == [
        {"account_alias": alias, "account_kind": "agentic", "label": "agentic", "alias_scheme": "hmac_sha256_v1"}
    ]


def test_tax_lots_reject_raw_identifiers_and_store_alias_and_hashed_lot_key(ledger, salt, db_path):
    from market_data_ingest.ledger.accounts import account_alias
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError):
        ledger.ingest_tax_lots([_lot(RAW_A)], source="broker_tax_lots", run_id="r", as_of=FIRST_SEEN)
    with pytest.raises(LedgerValidationError):
        ledger.ingest_tax_lots([_lot(account_alias(RAW_A), account=RAW_A)], source="broker_tax_lots", run_id="r",
                               as_of=FIRST_SEEN)
    with pytest.raises(LedgerValidationError):  # raw digits smuggled in as a lot id
        ledger.ingest_tax_lots([_lot(account_alias(RAW_A), lot_id=RAW_B)], source="broker_tax_lots", run_id="r",
                               as_of=FIRST_SEEN)
    assert ledger.row_count("tax_lots") == 0

    alias_a, alias_b = account_alias(RAW_A), account_alias(RAW_B)
    # A lot that moved from the agentic account to the main one: same broker lot id,
    # two snapshots, two aliases. It stays traceable through the hashed lot_key.
    lots = [_lot(alias_a), _lot(alias_b, snapshot_date=date(2026, 4, 15))]
    assert ledger.ingest_tax_lots(lots, source="broker_tax_lots", run_id="r", as_of=FIRST_SEEN) == 2
    stored = ledger.tax_lots(known_at=utc(2026, 6, 1))
    assert {r["account_alias"] for r in stored} == {alias_a, alias_b}
    assert len({r["lot_key"] for r in stored}) == 1
    assert "lot_id" not in stored[0]


def test_no_raw_identifier_or_salt_reaches_any_table(ledger, salt):
    from market_data_ingest.ledger.accounts import account_alias

    alias = account_alias(RAW_A)
    ledger.ingest_accounts([{"account_alias": alias, "account_kind": "agentic"}], source="t", run_id="r",
                           as_of=FIRST_SEEN)
    ledger.ingest_tax_lots([_lot(alias)], source="broker_tax_lots", run_id="r", as_of=FIRST_SEEN)
    for table in ("accounts", "tax_lots"):
        cols = ledger.table_columns(table)
        for row in ledger.con.execute(f"SELECT * FROM {table}").fetchall():
            text = " ".join(str(v) for v in row)
            assert RAW_A not in text, f"{table} leaked the raw identifier"
            assert TEST_SALT not in text, f"{table} leaked the salt"
            assert "lot-aaaa-bbbb" not in text, f"{table} kept the raw broker lot id"
        assert not {"account_number", "account_id", "salt"} & set(cols)


def test_guard_matches_the_repo_secrets_scan_shapes():
    # The ledger guard must reject at least every shape tests/test_config_invariants.py
    # treats as account-shaped: a bare 6-12 digit run and a labelled one.
    from market_data_ingest.ledger.accounts import looks_like_account_identifier

    for value in ("4" * 6, "4" * 9, "4" * 12, f"rh_{'4' * 9}", f"Account: {'7' * 8}", f"broker{'7' * 8}"):
        assert looks_like_account_identifier(value), value
    for value in ("ZZZA", "lot-aaaa-bbbb", "agentic", "2026-03-02", "15:30"):
        assert not looks_like_account_identifier(value), value
