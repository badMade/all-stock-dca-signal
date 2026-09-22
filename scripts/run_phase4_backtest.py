"""Script entry point for running Phase 4 DCA Verification & Tier C Backtest."""

from __future__ import annotations

import sys

from all_stock_dca.backtest import main

if __name__ == "__main__":
    sys.exit(main())
