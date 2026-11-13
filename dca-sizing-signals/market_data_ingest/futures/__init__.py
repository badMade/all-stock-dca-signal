"""Futures ingestion for the overnight gap tracker (docs/adr-overnight-futures-tracker.md, D3, D5).

The append-only, point-in-time store for ES/NQ/YM contracts and 1-minute bars. It never imports
overnight_futures (tests/test_architecture_boundaries.py): turning stored bars into the
tracker's OvernightInputs is a caller's job, outside both packages.
"""
