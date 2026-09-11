"""Tests for spec_writer.py.

Covers schema creation/idempotency, the three-question interview loop
(including blank/whitespace re-prompting), session id generation, SQLite
persistence, and load() round-tripping -- all against a real file-backed
SQLite db via pytest's tmp_path fixture (never :memory:, since each
:memory: connection is its own isolated database and spec_writer.py opens a
fresh connection per method call).
"""

import dataclasses
import inspect
import sqlite3
import uuid

import pytest
from spec_writer import SpecResult, SpecWriter

Q1 = "What does this do?"
Q2 = "What does this NOT do?"
Q3 = "What does done look like in one sentence?"

ANSWER_1 = "Crawls a URL and reports its top SEO issues."
ANSWER_2 = "It does not modify the site or auto-file tickets."
ANSWER_3 = "A ranked list of the top three SEO issues with fixes, plus DONE."


def scripted_input(answers):
    """Return a callable(prompt) -> str that yields each answer in order.

    Ignores the prompt text; raises StopIteration if called more times than
    there are scripted answers (a bug in the test, or in SpecWriter.run's
    re-prompt logic, should surface loudly rather than silently).
    """
    iterator = iter(answers)
    return lambda prompt: next(iterator)


def make_writer(tmp_path, answers, outputs=None, db_name="spec.db"):
    """Build a SpecWriter against a real file-backed db in tmp_path."""
    db_path = str(tmp_path / db_name)
    output_fn = outputs.append if outputs is not None else (lambda msg: None)
    return SpecWriter(db_path=db_path, input_fn=scripted_input(answers), output_fn=output_fn)


def test_init_creates_spec_table_with_expected_columns_and_index(tmp_path):
    db_path = str(tmp_path / "spec.db")
    SpecWriter(db_path=db_path, input_fn=scripted_input([]), output_fn=lambda msg: None)

    with sqlite3.connect(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(spec)").fetchall()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='spec'"
        ).fetchall()

    # PRAGMA table_info row shape: (cid, name, type, notnull, dflt_value, pk)
    by_name = {row[1]: row for row in columns}
    assert set(by_name) == {
        "id",
        "session_id",
        "what_it_does",
        "what_it_does_not",
        "done_looks_like",
        "created_at",
    }

    assert by_name["id"][2].upper() == "INTEGER"
    assert by_name["id"][5] == 1  # part of the primary key

    for name in ("session_id", "what_it_does", "what_it_does_not", "done_looks_like", "created_at"):
        assert by_name[name][2].upper() == "TEXT"
        assert by_name[name][3] == 1, f"{name} should be NOT NULL"

    assert ("idx_spec_session",) in indexes


def test_init_is_idempotent(tmp_path):
    db_path = str(tmp_path / "spec.db")
    SpecWriter(db_path=db_path, input_fn=scripted_input([]), output_fn=lambda msg: None)
    # Second construction against the same path must not raise, and must not
    # duplicate the table.
    SpecWriter(db_path=db_path, input_fn=scripted_input([]), output_fn=lambda msg: None)

    with sqlite3.connect(db_path) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spec'"
        ).fetchall()
    assert len(tables) == 1


def test_run_returns_specresult_with_correct_fields(tmp_path):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3])

    result = writer.run()

    assert result.what_it_does == ANSWER_1
    assert result.what_it_does_not == ANSWER_2
    assert result.done_looks_like == ANSWER_3


def test_run_generates_valid_uuid4_session_id(tmp_path):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3])

    result = writer.run()

    parsed = uuid.UUID(result.session_id)
    assert parsed.version == 4


def test_two_run_calls_produce_different_session_ids(tmp_path):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3, ANSWER_1, ANSWER_2, ANSWER_3])

    first = writer.run()
    second = writer.run()

    assert first.session_id != second.session_id


def test_run_persists_row_to_sqlite(tmp_path):
    db_path = str(tmp_path / "spec.db")
    writer = SpecWriter(
        db_path=db_path,
        input_fn=scripted_input([ANSWER_1, ANSWER_2, ANSWER_3]),
        output_fn=lambda msg: None,
    )

    result = writer.run()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT what_it_does, what_it_does_not, done_looks_like, session_id, created_at "
            "FROM spec WHERE session_id = ?",
            (result.session_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == ANSWER_1
    assert row[1] == ANSWER_2
    assert row[2] == ANSWER_3
    assert row[3] == result.session_id
    assert row[4]  # created_at is non-empty


def test_empty_answer_blocks_advancement_and_reprompts(tmp_path):
    outputs = []
    writer = make_writer(tmp_path, ["", ANSWER_1, ANSWER_2, ANSWER_3], outputs=outputs)

    result = writer.run()

    assert result.what_it_does == ANSWER_1
    assert len(outputs) == 1
    assert "missing answer for:" in outputs[0]
    assert Q1 in outputs[0]


@pytest.mark.parametrize("blank", [" ", "\t\n"])
def test_whitespace_only_answer_blocks_advancement(tmp_path, blank):
    outputs = []
    writer = make_writer(tmp_path, [blank, ANSWER_1, ANSWER_2, ANSWER_3], outputs=outputs)

    result = writer.run()

    assert result.what_it_does == ANSWER_1
    assert len(outputs) == 1
    assert "missing answer for:" in outputs[0]
    assert Q1 in outputs[0]


def test_each_question_blocks_independently_single_message_for_second(tmp_path):
    outputs = []
    writer = make_writer(tmp_path, [ANSWER_1, "", ANSWER_2, ANSWER_3], outputs=outputs)

    result = writer.run()

    assert result.what_it_does == ANSWER_1
    assert result.what_it_does_not == ANSWER_2
    assert result.done_looks_like == ANSWER_3
    assert len(outputs) == 1
    assert "missing answer for:" in outputs[0]
    assert Q2 in outputs[0]
    assert Q1 not in outputs[0]
    assert Q3 not in outputs[0]


def test_load_returns_matching_specresult(tmp_path):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3])
    result = writer.run()

    loaded = writer.load(result.session_id)

    assert loaded == result


def test_load_returns_none_for_unknown_session(tmp_path):
    writer = make_writer(tmp_path, [])

    assert writer.load(str(uuid.uuid4())) is None


def test_specresult_is_frozen():
    result = SpecResult(
        what_it_does=ANSWER_1,
        what_it_does_not=ANSWER_2,
        done_looks_like=ANSWER_3,
        session_id=str(uuid.uuid4()),
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.what_it_does = "changed"


def test_load_from_second_writer_instance_same_db(tmp_path):
    db_path = str(tmp_path / "spec.db")
    writer1 = SpecWriter(
        db_path=db_path,
        input_fn=scripted_input([ANSWER_1, ANSWER_2, ANSWER_3]),
        output_fn=lambda msg: None,
    )
    result = writer1.run()

    writer2 = SpecWriter(db_path=db_path, input_fn=scripted_input([]), output_fn=lambda msg: None)
    loaded = writer2.load(result.session_id)

    assert loaded == result


def test_default_input_output_fn_are_builtins():
    params = inspect.signature(SpecWriter.__init__).parameters
    assert params["input_fn"].default is input
    assert params["output_fn"].default is print


# -- Regression coverage for the 2026-07-28 review findings -----------------
# _ask() hardening (EOFError handling, bounded retries) and explicit SQLite
# connection closing. Added per the review's TDD-for-defects requirement:
# these are written first, confirmed to fail against the prior
# implementation, then made to pass.


def test_ask_raises_runtime_error_on_eof(tmp_path):
    def raising_input(prompt):
        raise EOFError

    writer = SpecWriter(
        db_path=str(tmp_path / "spec.db"), input_fn=raising_input, output_fn=lambda msg: None
    )

    with pytest.raises(RuntimeError, match="What does this do"):
        writer.run()


def test_ask_raises_runtime_error_after_default_max_attempts(tmp_path):
    outputs = []
    # Exactly 10 blanks: the default max_attempts, so the 1st question's
    # _ask() call exhausts its budget before ever reaching questions 2/3.
    writer = make_writer(tmp_path, [""] * 10, outputs=outputs)

    with pytest.raises(RuntimeError, match="Exceeded maximum prompt attempts"):
        writer.run()

    assert len(outputs) == 10


def test_ask_direct_raises_after_custom_max_attempts(tmp_path):
    writer = make_writer(tmp_path, ["", ""])

    with pytest.raises(RuntimeError, match="Exceeded maximum prompt attempts"):
        writer._ask("custom question", max_attempts=2)


def test_ask_direct_succeeds_within_custom_max_attempts(tmp_path):
    writer = make_writer(tmp_path, ["", "real answer"])

    assert writer._ask("custom question", max_attempts=2) == "real answer"


def _spy_on_connections(monkeypatch):
    """Patch sqlite3.connect to record every Connection object it creates."""
    connections = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    return connections


def test_init_db_closes_its_connection(tmp_path, monkeypatch):
    connections = _spy_on_connections(monkeypatch)

    SpecWriter(db_path=str(tmp_path / "spec.db"), input_fn=scripted_input([]), output_fn=lambda msg: None)

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_save_closes_its_connection(tmp_path, monkeypatch):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3])
    connections = _spy_on_connections(monkeypatch)

    writer.run()

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_load_closes_its_connection(tmp_path, monkeypatch):
    writer = make_writer(tmp_path, [ANSWER_1, ANSWER_2, ANSWER_3])
    result = writer.run()
    connections = _spy_on_connections(monkeypatch)

    writer.load(result.session_id)

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")
