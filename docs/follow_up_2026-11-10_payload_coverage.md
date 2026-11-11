# Follow-up: 9:35 payload refused on coverage floor (2026-11-10)

Status: open, noted at the operator's request. Do not treat as resolved.

What happened: the Robinhood payload job for the 0935 slot got 429 Too Many Requests on many chunks, so 60/122 tickers (49%) validated. The publisher's 95% coverage floor (DEFAULT_MIN_COVERAGE) refused to publish, so Gate 5 had no market data and the slot placed no orders. Evidence: cron_tracking/robinhood_payload/2026-11-10/*.refused.json and the chunk_logs folder.

To decide later:
1. Why the job hit rate limits at 09:36-09:40 (chunk size, parallelism, retries/backoff).
2. Whether a hard 95% floor should block the slot, or whether a lower floor / partial-payload mode (missing tickers score NO_DATA and sit out) is acceptable. The generator already treats failed tickers as NO_DATA, never BUY.
3. Whether the executor session should be allowed a bounded rebuild/retry of the payload job when the first build is refused.
