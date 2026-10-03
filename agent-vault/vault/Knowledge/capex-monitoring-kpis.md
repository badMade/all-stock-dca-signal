# capex monitoring KPIs

STATUS: skeleton. Initial watchlist populated from universe on 2026-10-03. The `analyze-capex` skill reads it first.

## Watched companies

### Operating Companies (SEC 10-Q / 10-K / 20-F Filers)
- AAPL (Apple Inc.): "Payments for acquisition of property, plant and equipment", fiscal year end: September 30.
- ABBV (AbbVie Inc.): "Acquisitions of property, plant and equipment", fiscal year end: December 31.
- AMAT (Applied Materials, Inc.): "Capital expenditures", fiscal year end: October.
- AMD (Advanced Micro Devices, Inc.): "Purchases of property, plant and equipment", fiscal year end: December.
- AMZN (Amazon.com, Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- ANET (Arista Networks, Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- ASML (ASML Holding N.V.): "Capital expenditures", fiscal year end: December 31 (Form 20-F).
- AVGO (Broadcom Inc.): "Purchases of property, plant and equipment", fiscal year end: October.
- BYDDY (BYD Company Ltd): "Purchases of property, plant and equipment", fiscal year end: December 31 (ADR).
- CAT (Caterpillar Inc.): "Capital expenditures", fiscal year end: December 31.
- CBRS (Cerebras Systems Inc.): Private / S-1 filing status, fiscal year end: December 31.
- CIEN (Ciena Corporation): "Purchases of property, plant and equipment", fiscal year end: October 31.
- COHR (Coherent Corp.): "Purchases of property, plant and equipment", fiscal year end: June 30.
- CSCO (Cisco Systems, Inc.): "Capital expenditures", fiscal year end: July.
- DFTX (Definium Therapeutics, Inc.): Operating biopharma capex, fiscal year end: December 31.
- EVR (Evercore, Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- FFIV (F5, Inc.): "Capital expenditures", fiscal year end: September 30.
- GE (GE Aerospace): "Additions to property, plant and equipment", fiscal year end: December 31.
- GFUZ (General Fusion Group Ltd.): Clean energy research and capital assets, fiscal year end: December 31.
- GLW (Corning Incorporated): "Capital expenditures", fiscal year end: December 31.
- GOOGL / GOOG (Alphabet Inc.): "Capital expenditures", fiscal year end: December 31.
- GS (The Goldman Sachs Group, Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- IBM (International Business Machines Corp.): "Payments for property, plant and equipment", fiscal year end: December 31.
- INTC (Intel Corporation): "Additions to property, plant and equipment", fiscal year end: December.
- IRDM (Iridium Communications, Inc.): "Purchases of property, plant and equipment", fiscal year end: December 31.
- JPM (JPMorgan Chase & Co.): "Capital expenditures", fiscal year end: December 31.
- LITE (Lumentum Holdings, Inc.): "Purchases of property, plant and equipment", fiscal year end: June.
- LLY (Eli Lilly and Company): "Additions to property and equipment", fiscal year end: December 31.
- LUNR (Intuitive Machines, Inc.): "Purchases of property, plant and equipment", fiscal year end: December 31.
- MA (Mastercard Incorporated): "Purchases of property and equipment", fiscal year end: December 31.
- MRVL (Marvell Technology, Inc.): "Purchases of property, plant and equipment", fiscal year end: January.
- MSFT (Microsoft Corporation): "Additions to property and equipment", fiscal year end: June 30.
- MU (Micron Technology, Inc.): "Expenditures for property, plant and equipment", fiscal year end: August.
- NFLX (Netflix, Inc.): "Additions to property, plant and equipment", fiscal year end: December 31.
- NVDA (NVIDIA Corporation): "Purchases of property and equipment and intangible assets", fiscal year end: January.
- QBTS (D-Wave Quantum Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- QCOM (Qualcomm Incorporated): "Capital expenditures", fiscal year end: September.
- QUBT (Quantum Computing Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- RGTI (Rigetti Computing, Inc.): "Purchases of property and equipment", fiscal year end: December 31.
- RKLB (Rocket Lab USA, Inc.): "Purchases of property, plant and equipment", fiscal year end: December 31.
- SNDK (Sandisk Corporation): Storage hardware capex, fiscal year end: June.
- SPCX (Space Exploration Technologies Corp.): Private equity holding, non-SEC reporting.
- STM (STMicroelectronics N.V.): "Capital expenditures", fiscal year end: December 31 (Form 20-F).
- STX (Seagate Technology Holdings plc): "Payments for property, plant and equipment", fiscal year end: June.
- TRLV (Trulieve Cannabis Corp.): "Capital expenditures", fiscal year end: December 31.
- TSLA (Tesla, Inc.): "Capital expenditures", fiscal year end: December 31.
- TSM (Taiwan Semiconductor Manufacturing Co.): "Capital expenditures", fiscal year end: December 31 (Form 20-F).
- TXN (Texas Instruments Incorporated): "Capital expenditures", fiscal year end: December 31.
- VIAV (Viavi Solutions Inc.): "Purchases of property, plant and equipment", fiscal year end: June.
- VRT (Vertiv Holdings Co): "Capital expenditures", fiscal year end: December 31.
- WDC (Western Digital Corporation): "Purchases of property, plant and equipment", fiscal year end: June.

### Non-Operating Vehicles (Excluded from Capex Analysis)
These symbols are ETFs, closed-end funds, venture trusts, or preferred stock; they do not report corporate property/plant capex:
- AAAU: Goldman Sachs Physical Gold ETF (Commodity trust)
- BTX: BlackRock Technology and Private Equity Term Trust (Closed-end fund)
- DRAM: Roundhill Memory ETF (Index ETF)
- DXYZ: Destiny Tech100, Inc. (Closed-end investment fund)
- FLKR: Franklin FTSE South Korea ETF (Foreign equity ETF)
- QQQ: Invesco QQQ Trust (Index ETF)
- QTOP: iShares Nasdaq Top 30 Stocks ETF (Index ETF)
- RVI: Robinhood Ventures Fund I (Venture fund)
- RVII: Robinhood Ventures Fund II (Venture fund)
- SATA: Strive, Inc. Variable Rate Series A Perpetual Preferred Stock (Preferred shares)
- TOPT: iShares Top 20 U.S. Stocks ETF (Index ETF)
- TRIN: Trinity Capital, Inc. (Business Development Company)
- VCX: Fundrise Innovation Fund, LLC (Interval fund)
- VT: Vanguard Total World Stock ETF (Index ETF)
- XLG: Invesco S&P 500 Top 50 ETF (Index ETF)

## Thresholds

Live in `config.yaml`: `capex_delta_flag_pct` (verified 2026-10-03 against the baseline run) and
`capex_research_addition_usd_m` (settled 2026-10-03 under the two-tier rule: $2,000M for hyperscalers
over $5B quarterly capex, $250M for suppliers and mid-caps). See `threshold-review.md`.

## Baselines

| Company | Period end | Capex (unit) | Source (accession or URL) |
| --- | --- | --- | --- |
| NVDA | 2025-07-27 | $1,227M (6-mo YTD) | Form 10-Q (SEC EDGAR) |
| MSFT | 2026-06-30 | $35,802M (Q4) / $115,948M (FY26) | Form 10-K / 10-Q (SEC EDGAR) |
| GOOGL | 2026-06-30 | $44,900M (Q2) / $80,600M (6-mo YTD) | Form 10-Q (SEC EDGAR) |
| AMZN | 2026-06-30 | $53,100M (Q2) / $96,300M (6-mo YTD) | Form 10-Q (SEC EDGAR) |
| TSM | 2025-12-31 | US$40.9B (NT$1,272,411M) | Form 20-F (SEC EDGAR) |
| ASML | 2025-12-31 | €1,573.6M | Form 20-F (SEC EDGAR) |
| AMAT | 2026-07-26 | $2,000M (9-mo YTD) | Form 10-Q (SEC EDGAR) |
| AVGO | 2026-08-02 | $1,013M (9-mo YTD) | Form 10-Q (SEC EDGAR) |

## Sources the skill may read

Read-only only: Robinhood SEC filing tools (`get_sec_filing_index`, `get_sec_filing_facts`, `get_sec_filing`),
Fiscal.ai, Bigdata.com, a filing the owner supplies, and Notion for reading. No source, no number.

## Prior findings

- 2026-10-03: Baseline extraction for MSFT, GOOGL, AMZN, and NVDA.
  - `capex_delta_flag_pct: 15%` verified effective: flagged MSFT Q1->Q2 (+54.0%), MSFT Q3->Q4 (+15.9%), GOOGL Q1->Q2 (+25.8%), and AMZN Q1->Q2 (+20.1%), while passing MSFT Q2->Q3 (+3.3%).
  - `capex_research_addition_usd_m: 500M` found scale-blind: hyperscalers shift capex by billions per quarter, so $500M trips on every quarter (~1-2% variance). Recommended tiering or relative threshold in threshold-review.md.
