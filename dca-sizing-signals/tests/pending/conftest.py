"""Keep pytest from collecting the RED TDD tests in this folder.

These tests were written first, per the repo's TDD-first policy, for modules
that do not exist yet (``market_data_ingest/edgar_fundamentals.py`` and
``market_data_ingest/dolthub_options.py``). They fail on import until those
modules are written, and writing them waits on the operator's approval.

They used to sit in ``tests/`` beside the green suite, so a plain
``python -m pytest tests/`` stopped at collection. That was harmless while no
CI job ran this subproject. Once ``ci.yml`` gained a ``dca-sizing-signals``
job (2026-11-13), it would have kept the job red on every PR and on the daily
scheduled run of main, which self-heal reacts to. The operator chose to park
the files here rather than hide an ``--ignore`` in CI config: the exclusion
sits beside the tests it excludes.

To work on one, run it by path with this guard bypassed::

    python -m pytest --noconftest tests/pending/test_edgar_fundamentals.py

When its module exists and the test is green, ``git mv`` it back to
``tests/`` and delete this folder once it is empty.
"""

collect_ignore_glob = ["test_*.py"]
