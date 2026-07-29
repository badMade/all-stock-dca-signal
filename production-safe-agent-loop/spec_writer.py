"""Forces three questions to be answered before anything else runs.

SpecWriter interviews the operator for three one-sentence answers -- what a
task does, what it explicitly does not do, and what "done" looks like --
before an agent loop is allowed to start, and persists the answers to
SQLite keyed by a fresh session id.
"""

import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS spec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    what_it_does TEXT NOT NULL,
    what_it_does_not TEXT NOT NULL,
    done_looks_like TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_spec_session ON spec(session_id);"

_QUESTIONS = (
    ("what_it_does", "What does this do?"),
    ("what_it_does_not", "What does this NOT do?"),
    ("done_looks_like", "What does done look like in one sentence?"),
)


@dataclass(frozen=True)
class SpecResult:
    """The three-question spec for a task, plus the session id it was saved under.

    Attributes:
        what_it_does: One sentence describing what the task does.
        what_it_does_not: One sentence describing what the task explicitly
            does not do.
        done_looks_like: One sentence describing what "done" looks like.
        session_id: UUID4 string identifying the session this spec was
            collected under.
    """

    what_it_does: str
    what_it_does_not: str
    done_looks_like: str
    session_id: str


class SpecWriter:
    """Interviews an operator for a task spec and persists it to SQLite.

    Instantiating SpecWriter immediately opens `db_path` and creates the
    `spec` table and its session-id index if they don't already exist, so
    constructing it more than once against the same path is a no-op rather
    than an error.
    """

    def __init__(
        self,
        db_path: str = "spec.db",
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        """Initialize the writer and ensure the backing schema exists.

        Args:
            db_path: Path to the SQLite database file.
            input_fn: Callable taking a prompt string and returning the raw
                answer. Defaults to the builtin `input`.
            output_fn: Callable taking a message string, used to report
                re-prompts for blank answers. Defaults to the builtin `print`.

        Side effects:
            Opens `db_path` and runs `CREATE TABLE IF NOT EXISTS` plus the
            supporting index.
        """
        self.db_path = db_path
        self.input_fn = input_fn
        self.output_fn = output_fn
        self._init_db()

    def _init_db(self) -> None:
        """Create the spec table and its session-id index if missing."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(_CREATE_TABLE)
                conn.execute(_CREATE_INDEX)

    def _ask(self, question: str, max_attempts: int = 10) -> str:
        """Prompt for `question`, re-prompting until a non-blank answer arrives.

        Args:
            question: The question text passed to `input_fn` as the prompt.
            max_attempts: Maximum number of prompts before giving up.

        Returns:
            The stripped, non-empty answer.

        Raises:
            RuntimeError: If `input_fn` raises `EOFError` (real end of input,
                e.g. a closed/redirected stream), or if `max_attempts`
                consecutive blank answers are received without ever raising.

        Side effects:
            Calls `input_fn` at least once, and calls `output_fn` with a
            "missing answer for: <question>" message on every blank or
            whitespace-only attempt.
        """
        for _ in range(max_attempts):
            try:
                answer = self.input_fn(question).strip()
            except EOFError as err:
                raise RuntimeError(f"Unexpected end of input while prompting for: {question}") from err
            if answer:
                return answer
            self.output_fn(f"missing answer for: {question}")
        raise RuntimeError(f"Exceeded maximum prompt attempts for question: {question}")

    def run(self) -> SpecResult:
        """Collect the three-question spec and persist it under a fresh session id.

        Returns:
            The `SpecResult` that was just saved.

        Side effects:
            Prompts via `input_fn` (re-prompting via `output_fn` on blank
            answers) and writes one row to `db_path`.
        """
        answers = {field: self._ask(question) for field, question in _QUESTIONS}
        result = SpecResult(session_id=str(uuid.uuid4()), **answers)
        self._save(result)
        return result

    def _save(self, result: SpecResult) -> None:
        """Insert one row for `result`, stamped with the current UTC time.

        Side effects:
            Writes one row to `db_path` with `created_at` set to
            `datetime.now(timezone.utc).isoformat()`.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO spec "
                    "(session_id, what_it_does, what_it_does_not, done_looks_like, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        result.session_id,
                        result.what_it_does,
                        result.what_it_does_not,
                        result.done_looks_like,
                        created_at,
                    ),
                )

    def load(self, session_id: str) -> Optional[SpecResult]:
        """Load the most recently saved spec for `session_id`.

        Args:
            session_id: The session id to look up.

        Returns:
            The matching `SpecResult`, or `None` if no row matches.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT what_it_does, what_it_does_not, done_looks_like, session_id "
                "FROM spec WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        what_it_does, what_it_does_not, done_looks_like, row_session_id = row
        return SpecResult(
            what_it_does=what_it_does,
            what_it_does_not=what_it_does_not,
            done_looks_like=done_looks_like,
            session_id=row_session_id,
        )
