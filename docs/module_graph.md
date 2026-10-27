# Module graph (generated)

Do not edit by hand. Regenerate with `python -m scripts.generate_module_graph`; `--check` exits 1 when this copy is stale and `--impact <module>` lists everything that depends on a module. A workflow refreshes this file on every push to `main`.

168 core modules, 519 in-repo import edges. Scope: top-level `*.py` plus `scripts/`, `all_stock_dca/`, `guardrails/`, `selfheal/`, `viz/`; tests and the independent research trees are excluded. Static imports only: string dispatch is invisible, so "no importer" is a lead, not a verdict (see the external-refs column).

## Most depended-on modules (change these with care)

| Module | Imported by | Direct importers |
| --- | ---: | --- |
| `dca_paths` | 45 | `dca_alerts`, `dca_console`, `dca_console_alerts`, `dca_console_launch`, `dca_console_runs`, `dca_cumulative`, +39 more |
| `dca_fileio` | 40 | `dca_alerts`, `dca_average_cost_snapshot`, `dca_console`, `dca_console_jobs`, `dca_console_launch`, `dca_cost_tracker`, +34 more |
| `dca_config` | 35 | `dca_console_alerts`, `dca_console_config`, `dca_console_octopus`, `dca_console_runs`, `dca_console_state`, `dca_fill_compare`, +29 more |
| `near_close_dca` | 32 | `dca_alerts`, `dca_config`, `dca_console_config`, `dca_console_jobs`, `dca_live_adapters`, `dca_market_graph`, +26 more |
| `dca_runner` | 23 | `dca_console`, `dca_console_config`, `dca_console_data`, `dca_console_schedule`, `dca_console_state`, `dca_order_plan`, +17 more |
| `dca_console_reader` | 16 | `dca_console`, `dca_console_alerts`, `dca_console_config`, `dca_console_docs`, `dca_console_env`, `dca_console_launch`, +10 more |
| `scripts.file_lock` | 12 | `dca_cumulative`, `dca_intraday_archive`, `dca_inverse_martingale`, `dca_whole_share_accumulator`, `morning_dca`, `near_close_dca`, +6 more |
| `scripts.run_classification` | 12 | `dca_console_runs`, `dca_cost_tracker`, `dca_reporting`, `dca_risk`, `morning_dca`, `near_close_dca`, +6 more |
| `dca_rec_models` | 11 | `dca_fill_compare`, `dca_market_history`, `dca_market_sources`, `dca_rec_publish`, `dca_rec_store`, `dca_recommender`, +5 more |
| `dca_live_adapters` | 9 | `dca_market_graph`, `dca_market_history`, `dca_runner`, `near_close_dca`, `run_dca`, `scripts.assemble_market_data`, +3 more |
| `dca_risk` | 9 | `dca_console_alerts`, `dca_console_data`, `dca_console_launch`, `dca_console_runs`, `dca_console_state`, `dca_recommender`, +3 more |
| `dca_ticker_paths` | 9 | `dca_console_tickers`, `dca_intraday_archive`, `dca_inverse_martingale`, `dca_market_graph`, `scripts.daily_per_ticker_calibration`, `scripts.generate_per_ticker_sizing`, +3 more |
| `scripts.scheduled_artifacts` | 9 | `dca_console_data`, `dca_console_runs`, `dca_console_schedule`, `morning_dca`, `near_close_dca`, `run_dca`, +3 more |
| `dca_console_jobs` | 8 | `dca_console`, `dca_console_alerts`, `dca_console_config`, `dca_console_env`, `dca_console_routes`, `dca_console_runs`, +2 more |
| `dca_order_plan` | 8 | `dca_execution_router`, `dca_number_check`, `dca_recommender`, `scripts.backup_gates`, `scripts.backup_planner`, `scripts.backup_procedure`, +2 more |

## No in-repo importer

Entry points (CLIs, executor classes driven by an external agent) land here by design. `External refs` counts mentions outside Python (live prompts, workflows, `pyproject.toml`, config); 0 refs and 0 importers is a dead-code candidate.

| Module | Lines | External refs |
| --- | ---: | ---: |
| `all_stock_dca` | 3 | 0 |
| `all_stock_dca.merge_predictions` | 90 | 0 |
| `all_stock_dca.train_forecaster` | 359 | 0 |
| `build_raw_data` | 98 | 0 |
| `dca_provenance` | 156 | 0 |
| `find_issues` | 16 | 0 |
| `from` | 30 | 3 |
| `guardrails` | 25 | 0 |
| `run_benchmark` | 56 | 0 |
| `run_market_open_signals` | 110 | 0 |
| `scripts` | 0 | 254 |
| `scripts.adhoc_orders` | 490 | 0 |
| `scripts.auto_resume_walkforward` | 179 | 0 |
| `scripts.backfill_afternoon_evidence` | 325 | 0 |
| `scripts.backfill_intraday_archive` | 215 | 0 |
| `scripts.backup_strategy` | 732 | 0 |
| `scripts.build_robinhood_market_data` | 84 | 0 |
| `scripts.check_slot_fills` | 120 | 7 |
| `scripts.cloud_preflight` | 280 | 0 |
| `scripts.compute_schedule` | 351 | 1 |
| `scripts.emit_order_plan` | 147 | 7 |
| `scripts.evaluate_deferral_params` | 502 | 0 |
| `scripts.evidence_publish` | 626 | 0 |
| `scripts.export_octopus_ledger` | 232 | 0 |
| `scripts.fill_perplexity_backup_prompts` | 298 | 2 |
| `scripts.generate_credit_usage_proxy_report` | 3321 | 7 |
| `scripts.generate_market_graph` | 369 | 2 |
| `scripts.generate_module_graph` | 251 | 3 |
| `scripts.generate_perplexity_backup_prompts` | 144 | 8 |
| `scripts.github_mcp_login` | 87 | 0 |
| `scripts.healthcheck` | 80 | 5 |
| `scripts.local_heartbeat` | 314 | 6 |
| `scripts.order_refetch` | 68 | 0 |
| `scripts.plan_first_slot` | 362 | 28 |
| `scripts.planner_stamp_check` | 185 | 2 |
| `scripts.prior_reference_bindings` | 94 | 2 |
| `scripts.promote_console` | 147 | 0 |
| `scripts.publish_octopus_sizing` | 398 | 0 |
| `scripts.read_recommendation` | 95 | 0 |
| `scripts.refresh_active_ticker_histories` | 199 | 1 |
| `scripts.refresh_market_open_candidates` | 158 | 0 |
| `scripts.robinhood_payload` | 899 | 21 |
| `scripts.run_90day_prediction_experiment` | 422 | 0 |
| `scripts.run_ledger` | 305 | 0 |
| `scripts.run_phase4_backtest` | 10 | 0 |
| `scripts.run_ticker_maintenance` | 132 | 4 |
| `scripts.self_heal` | 122 | 2 |
| `scripts.shard_heartbeat` | 133 | 0 |
| `scripts.snapshot_cycle` | 328 | 2 |
| `scripts.sync_live_prompts` | 228 | 0 |
| `scripts.verify_perplexity_pair` | 182 | 1 |
| `scripts.workflow_policy` | 230 | 3 |
| `scripts.workspace_probe` | 136 | 0 |
| `selfheal` | 5 | 0 |
| `sizing_adjustment_reader` | 102 | 0 |
| `snap` | 46 | 0 |

## All modules

| Module | Lines | Imports | Imported by | External refs | Imports (in-repo) |
| --- | ---: | ---: | ---: | ---: | --- |
| `all_stock_dca` | 3 | 0 | 0 | 0 |  |
| `all_stock_dca.backtest` | 495 | 0 | 1 | 5 |  |
| `all_stock_dca.merge_predictions` | 90 | 0 | 0 | 0 |  |
| `all_stock_dca.train_forecaster` | 359 | 0 | 0 | 0 |  |
| `build_raw_data` | 98 | 0 | 0 | 0 |  |
| `dca_alerts` | 603 | 5 | 4 | 4 | `dca_fileio`, `dca_net_utils`, `dca_paths`, `near_close_dca`, `scripts.idempotency` |
| `dca_average_cost_snapshot` | 207 | 1 | 1 | 0 | `dca_fileio` |
| `dca_bootstrap` | 220 | 0 | 2 | 0 |  |
| `dca_common` | 99 | 0 | 4 | 0 |  |
| `dca_config` | 1542 | 1 | 35 | 2 | `near_close_dca` |
| `dca_console` | 1539 | 12 | 4 | 2 | `dca_bootstrap`, `dca_console_data`, `dca_console_jobs`, `dca_console_launch`, `dca_console_octopus`, `dca_console_reader`, `dca_console_release`, `dca_console_routes`, +4 more |
| `dca_console_alerts` | 650 | 12 | 1 | 0 | `dca_alerts`, `dca_config`, `dca_console_jobs`, `dca_console_reader`, `dca_console_runs`, `dca_paths`, `dca_risk`, `scripts.alert_digest`, +4 more |
| `dca_console_config` | 821 | 5 | 1 | 0 | `dca_config`, `dca_console_jobs`, `dca_console_reader`, `dca_runner`, `near_close_dca` |
| `dca_console_data` | 450 | 5 | 3 | 1 | `dca_cost_tracker`, `dca_risk`, `dca_runner`, `scripts.purchase_history`, `scripts.scheduled_artifacts` |
| `dca_console_diagnostics` | 621 | 0 | 1 | 0 |  |
| `dca_console_docs` | 144 | 1 | 1 | 0 | `dca_console_reader` |
| `dca_console_env` | 223 | 4 | 1 | 0 | `dca_console`, `dca_console_jobs`, `dca_console_reader`, `dca_console_release` |
| `dca_console_jobs` | 507 | 3 | 8 | 0 | `dca_console_routes`, `dca_fileio`, `near_close_dca` |
| `dca_console_launch` | 626 | 6 | 3 | 0 | `dca_console`, `dca_console_reader`, `dca_console_state`, `dca_fileio`, `dca_paths`, `dca_risk` |
| `dca_console_octopus` | 411 | 2 | 1 | 0 | `dca_config`, `dca_console_reader` |
| `dca_console_reader` | 190 | 0 | 16 | 0 |  |
| `dca_console_release` | 59 | 0 | 4 | 0 |  |
| `dca_console_reports` | 259 | 5 | 1 | 0 | `dca_average_cost_snapshot`, `dca_console_reader`, `dca_cost_tracker`, `dca_notion_sync`, `dca_weekly_dashboard` |
| `dca_console_research` | 116 | 2 | 1 | 0 | `dca_console_launch`, `dca_console_reader` |
| `dca_console_routes` | 653 | 17 | 2 | 0 | `dca_console`, `dca_console_alerts`, `dca_console_config`, `dca_console_diagnostics`, `dca_console_docs`, `dca_console_env`, `dca_console_jobs`, `dca_console_launch`, +9 more |
| `dca_console_runs` | 790 | 13 | 2 | 0 | `dca_config`, `dca_console_data`, `dca_console_jobs`, `dca_console_reader`, `dca_paths`, `dca_reporting`, `dca_risk`, `scripts.close_window_signal`, +5 more |
| `dca_console_schedule` | 739 | 11 | 1 | 0 | `dca_console_data`, `dca_console_jobs`, `dca_console_reader`, `dca_runner`, `intraday_dca`, `market_open_dca`, `midday_dca`, `morning_dca`, +3 more |
| `dca_console_state` | 903 | 6 | 2 | 0 | `dca_config`, `dca_console_jobs`, `dca_console_reader`, `dca_risk`, `dca_runner`, `scripts.purchase_history` |
| `dca_console_tickers` | 245 | 2 | 1 | 0 | `dca_console_reader`, `dca_ticker_paths` |
| `dca_console_training` | 355 | 1 | 1 | 0 | `dca_console_reader` |
| `dca_cost_tracker` | 413 | 3 | 2 | 0 | `dca_common`, `dca_fileio`, `scripts.run_classification` |
| `dca_cumulative` | 1555 | 3 | 2 | 0 | `dca_fileio`, `dca_paths`, `scripts.file_lock` |
| `dca_env` | 157 | 0 | 3 | 0 |  |
| `dca_execution_router` | 328 | 2 | 1 | 1 | `dca_order_plan`, `dca_paths` |
| `dca_fileio` | 392 | 0 | 40 | 0 |  |
| `dca_fill_compare` | 170 | 6 | 2 | 0 | `dca_config`, `dca_fileio`, `dca_rec_models`, `dca_rec_store`, `scripts.broker_reconciliation`, `scripts.validate_native_placement` |
| `dca_intraday_archive` | 644 | 3 | 4 | 0 | `dca_fileio`, `dca_ticker_paths`, `scripts.file_lock` |
| `dca_inverse_martingale` | 3317 | 4 | 1 | 0 | `dca_fileio`, `dca_paths`, `dca_ticker_paths`, `scripts.file_lock` |
| `dca_ladder_core` | 120 | 0 | 1 | 0 |  |
| `dca_live_adapters` | 8664 | 1 | 9 | 12 | `near_close_dca` |
| `dca_market_graph` | 2233 | 7 | 1 | 0 | `dca_config`, `dca_fileio`, `dca_intraday_archive`, `dca_live_adapters`, `dca_paths`, `dca_ticker_paths`, `near_close_dca` |
| `dca_market_history` | 296 | 7 | 3 | 0 | `dca_config`, `dca_fileio`, `dca_live_adapters`, `dca_market_sources`, `dca_rec_models`, `scripts.assemble_market_data`, `scripts.assemble_order_pages` |
| `dca_market_sources` | 119 | 3 | 4 | 0 | `dca_config`, `dca_rec_models`, `scripts.assemble_market_data` |
| `dca_net_utils` | 376 | 0 | 3 | 2 |  |
| `dca_notion_sync` | 103 | 1 | 2 | 0 | `dca_fileio` |
| `dca_number_check` | 226 | 1 | 1 | 0 | `dca_order_plan` |
| `dca_order_plan` | 1102 | 6 | 8 | 2 | `dca_config`, `dca_fileio`, `dca_paths`, `dca_runner`, `near_close_dca`, `scripts.validate_native_placement` |
| `dca_paths` | 219 | 1 | 45 | 0 | `dca_env` |
| `dca_provenance` | 156 | 1 | 0 | 0 | `dca_fileio` |
| `dca_rec_models` | 363 | 0 | 11 | 0 |  |
| `dca_rec_publish` | 223 | 1 | 2 | 0 | `dca_rec_models` |
| `dca_rec_store` | 277 | 5 | 8 | 0 | `dca_config`, `dca_fileio`, `dca_paths`, `dca_rec_models`, `scripts.validate_native_placement` |
| `dca_recommender` | 315 | 16 | 2 | 0 | `dca_config`, `dca_market_sources`, `dca_order_plan`, `dca_paths`, `dca_rec_models`, `dca_rec_store`, `dca_risk`, `dca_runner`, +8 more |
| `dca_reporting` | 1259 | 6 | 2 | 2 | `dca_fileio`, `dca_paths`, `scripts.close_window_signal`, `scripts.foreground_primary`, `scripts.model_usage_log`, `scripts.run_classification` |
| `dca_risk` | 1669 | 3 | 9 | 1 | `dca_config`, `near_close_dca`, `scripts.run_classification` |
| `dca_runner` | 9503 | 6 | 23 | 57 | `dca_config`, `dca_fileio`, `dca_live_adapters`, `dca_paths`, `dca_slot_guard`, `near_close_dca` |
| `dca_slot_guard` | 62 | 1 | 1 | 0 | `scripts.scheduler_lease` |
| `dca_ticker_paths` | 33 | 0 | 9 | 0 |  |
| `dca_weekly_dashboard` | 6476 | 2 | 1 | 12 | `dca_common`, `dca_fileio` |
| `dca_whole_share_accumulator` | 3826 | 4 | 1 | 3 | `dca_fileio`, `dca_paths`, `near_close_dca`, `scripts.file_lock` |
| `find_issues` | 16 | 0 | 0 | 0 |  |
| `from` | 30 | 2 | 0 | 3 | `dca_bootstrap`, `dca_console` |
| `guardrails` | 25 | 4 | 0 | 0 | `guardrails.engine`, `guardrails.input_guardrail`, `guardrails.output_guardrail`, `guardrails.retrieval_engine` |
| `guardrails.engine` | 284 | 3 | 1 | 0 | `guardrails.input_guardrail`, `guardrails.output_guardrail`, `guardrails.retrieval_engine` |
| `guardrails.input_guardrail` | 124 | 0 | 2 | 0 |  |
| `guardrails.output_guardrail` | 198 | 0 | 2 | 0 |  |
| `guardrails.retrieval_engine` | 168 | 0 | 2 | 0 |  |
| `intraday_dca` | 326 | 3 | 3 | 6 | `dca_paths`, `morning_dca`, `near_close_dca` |
| `market_open_dca` | 294 | 3 | 5 | 10 | `dca_paths`, `morning_dca`, `near_close_dca` |
| `midday_dca` | 525 | 4 | 4 | 9 | `dca_config`, `dca_paths`, `morning_dca`, `near_close_dca` |
| `morning_dca` | 9824 | 16 | 8 | 51 | `dca_alerts`, `dca_common`, `dca_config`, `dca_cumulative`, `dca_fileio`, `dca_intraday_archive`, `dca_paths`, `dca_risk`, +8 more |
| `near_close_dca` | 41712 | 17 | 32 | 26 | `dca_common`, `dca_cumulative`, `dca_fileio`, `dca_intraday_archive`, `dca_inverse_martingale`, `dca_ladder_core`, `dca_live_adapters`, `dca_paths`, +9 more |
| `run_benchmark` | 56 | 0 | 0 | 0 |  |
| `run_dca` | 4719 | 17 | 1 | 26 | `dca_alerts`, `dca_config`, `dca_fileio`, `dca_live_adapters`, `dca_notion_sync`, `dca_paths`, `dca_reporting`, `dca_runner`, +9 more |
| `run_market_open_signals` | 110 | 2 | 0 | 0 | `market_open_dca`, `near_close_dca` |
| `scripts` | 0 | 0 | 0 | 254 |  |
| `scripts.adhoc_orders` | 490 | 3 | 0 | 0 | `dca_paths`, `dca_runner`, `near_close_dca` |
| `scripts.agent_entry_points` | 694 | 0 | 1 | 1 |  |
| `scripts.alert_digest` | 5287 | 4 | 1 | 38 | `dca_config`, `dca_fileio`, `dca_paths`, `near_close_dca` |
| `scripts.assemble_market_data` | 312 | 2 | 2 | 0 | `dca_config`, `dca_live_adapters` |
| `scripts.assemble_order_pages` | 367 | 5 | 7 | 20 | `dca_config`, `dca_fileio`, `dca_live_adapters`, `scripts.broker_reconciliation`, `scripts.same_day_top_up_gate` |
| `scripts.auto_resume_walkforward` | 179 | 0 | 0 | 0 |  |
| `scripts.backfill_afternoon_evidence` | 325 | 3 | 0 | 0 | `dca_paths`, `dca_runner`, `scripts.finalize_run_evidence` |
| `scripts.backfill_intraday_archive` | 215 | 2 | 0 | 0 | `dca_intraday_archive`, `dca_paths` |
| `scripts.backup_gates` | 86 | 2 | 1 | 1 | `dca_order_plan`, `scripts.backup_procedure` |
| `scripts.backup_planner` | 1321 | 19 | 5 | 22 | `dca_config`, `dca_number_check`, `dca_order_plan`, `dca_paths`, `dca_rec_models`, `dca_rec_store`, `dca_runner`, `near_close_dca`, +11 more |
| `scripts.backup_procedure` | 330 | 6 | 3 | 1 | `dca_config`, `dca_order_plan`, `dca_rec_store`, `dca_runner`, `near_close_dca`, `scripts.backup_planner` |
| `scripts.backup_strategy` | 732 | 0 | 0 | 0 |  |
| `scripts.backup_stream_specs` | 36 | 5 | 2 | 1 | `scripts.generate_intraday_candidates`, `scripts.generate_market_open_candidates`, `scripts.generate_midday_candidates`, `scripts.generate_morning_candidates`, `scripts.stream_candidates` |
| `scripts.broker_reconciliation` | 1036 | 2 | 7 | 0 | `scripts.reason_codes`, `scripts.run_classification` |
| `scripts.build_backup_planner` | 352 | 0 | 1 | 1 |  |
| `scripts.build_robinhood_market_data` | 84 | 0 | 0 | 0 |  |
| `scripts.check_live_prompt_drift` | 485 | 1 | 1 | 9 | `dca_config` |
| `scripts.check_slot_fills` | 120 | 3 | 0 | 7 | `dca_console_reader`, `dca_paths`, `scripts.verify_schedule` |
| `scripts.close_window_signal` | 401 | 0 | 2 | 4 |  |
| `scripts.cloud_preflight` | 280 | 3 | 0 | 0 | `dca_config`, `dca_paths`, `scripts.verify_schedule` |
| `scripts.compute_schedule` | 351 | 0 | 0 | 1 |  |
| `scripts.correlation` | 85 | 0 | 1 | 0 |  |
| `scripts.daily_per_ticker_calibration` | 146 | 4 | 1 | 0 | `dca_fileio`, `dca_paths`, `dca_ticker_paths`, `scripts.generate_per_ticker_sizing` |
| `scripts.drift_detection` | 1332 | 2 | 5 | 56 | `dca_fileio`, `dca_net_utils` |
| `scripts.emit_order_plan` | 147 | 3 | 0 | 7 | `dca_config`, `dca_order_plan`, `dca_runner` |
| `scripts.evaluate_deferral_params` | 502 | 0 | 0 | 0 |  |
| `scripts.evidence_publish` | 626 | 2 | 0 | 0 | `dca_config`, `scripts.verify_schedule` |
| `scripts.export_octopus_ledger` | 232 | 1 | 0 | 0 | `scripts.sequestered_truth_comparator` |
| `scripts.file_lock` | 127 | 0 | 12 | 0 |  |
| `scripts.fill_perplexity_backup_prompts` | 298 | 1 | 0 | 2 | `dca_env` |
| `scripts.finalize_run_evidence` | 1168 | 13 | 2 | 36 | `dca_fileio`, `dca_paths`, `dca_runner`, `intraday_dca`, `market_open_dca`, `midday_dca`, `morning_dca`, `scripts.file_lock`, +5 more |
| `scripts.foreground_primary` | 620 | 0 | 1 | 5 |  |
| `scripts.gate_failure_diagnostics` | 352 | 3 | 1 | 0 | `dca_paths`, `dca_risk`, `scripts.same_day_top_up_gate` |
| `scripts.generate_buy_candidates` | 2105 | 3 | 5 | 11 | `dca_config`, `dca_runner`, `near_close_dca` |
| `scripts.generate_credit_usage_proxy_report` | 3321 | 5 | 0 | 7 | `dca_alerts`, `dca_paths`, `scripts.model_usage_log`, `scripts.run_classification`, `scripts.scheduled_artifacts` |
| `scripts.generate_intraday_candidates` | 200 | 3 | 2 | 17 | `intraday_dca`, `near_close_dca`, `scripts.stream_candidates` |
| `scripts.generate_market_graph` | 369 | 2 | 0 | 2 | `dca_fileio`, `dca_market_graph` |
| `scripts.generate_market_open_candidates` | 173 | 3 | 2 | 9 | `market_open_dca`, `near_close_dca`, `scripts.stream_candidates` |
| `scripts.generate_midday_candidates` | 152 | 3 | 2 | 15 | `midday_dca`, `near_close_dca`, `scripts.stream_candidates` |
| `scripts.generate_module_graph` | 251 | 0 | 0 | 3 |  |
| `scripts.generate_morning_candidates` | 172 | 3 | 2 | 7 | `morning_dca`, `near_close_dca`, `scripts.stream_candidates` |
| `scripts.generate_per_ticker_sizing` | 349 | 2 | 1 | 15 | `dca_fileio`, `dca_ticker_paths` |
| `scripts.generate_perplexity_backup_prompts` | 144 | 2 | 0 | 8 | `scripts.backup_procedure`, `scripts.build_backup_planner` |
| `scripts.github_mcp_login` | 87 | 0 | 0 | 0 |  |
| `scripts.healthcheck` | 80 | 0 | 0 | 5 |  |
| `scripts.idempotency` | 182 | 0 | 4 | 2 |  |
| `scripts.ingest_market_data` | 477 | 4 | 1 | 0 | `dca_config`, `dca_fileio`, `dca_live_adapters`, `scripts.generate_buy_candidates` |
| `scripts.local_heartbeat` | 314 | 4 | 0 | 6 | `dca_execution_router`, `dca_paths`, `scripts.file_lock`, `scripts.scheduler_lease` |
| `scripts.model_usage_log` | 330 | 1 | 4 | 3 | `dca_paths` |
| `scripts.order_refetch` | 68 | 4 | 0 | 0 | `dca_runner`, `scripts.assemble_order_pages`, `scripts.backup_planner`, `scripts.broker_reconciliation` |
| `scripts.plan_first_slot` | 362 | 8 | 0 | 28 | `dca_config`, `dca_market_history`, `dca_order_plan`, `dca_rec_models`, `dca_rec_store`, `dca_runner`, `scripts.assemble_order_pages`, `scripts.backup_planner` |
| `scripts.planner_stamp_check` | 185 | 1 | 0 | 2 | `scripts.schedule_watchdog` |
| `scripts.prior_reference_bindings` | 94 | 1 | 0 | 2 | `near_close_dca` |
| `scripts.promote_console` | 147 | 1 | 0 | 0 | `dca_console_release` |
| `scripts.publish_octopus_sizing` | 398 | 2 | 0 | 0 | `dca_config`, `dca_fileio` |
| `scripts.purchase_history` | 2002 | 5 | 2 | 0 | `dca_fileio`, `dca_live_adapters`, `dca_paths`, `near_close_dca`, `scripts.drift_detection` |
| `scripts.read_recommendation` | 95 | 3 | 0 | 0 | `dca_rec_models`, `dca_rec_store`, `scripts.backup_planner` |
| `scripts.reason_codes` | 224 | 0 | 4 | 1 |  |
| `scripts.refresh_active_ticker_histories` | 199 | 1 | 0 | 1 | `dca_ticker_paths` |
| `scripts.refresh_market_open_candidates` | 158 | 3 | 0 | 0 | `market_open_dca`, `near_close_dca`, `scripts.stream_candidates` |
| `scripts.robinhood_payload` | 899 | 18 | 0 | 21 | `dca_config`, `dca_fill_compare`, `dca_market_history`, `dca_market_sources`, `dca_paths`, `dca_rec_models`, `dca_rec_publish`, `dca_rec_store`, +10 more |
| `scripts.run_90day_prediction_experiment` | 422 | 1 | 0 | 0 | `scripts.sequestered_truth_comparator` |
| `scripts.run_classification` | 4499 | 4 | 12 | 28 | `dca_fileio`, `dca_paths`, `scripts.broker_reconciliation`, `scripts.same_day_pointer` |
| `scripts.run_ledger` | 305 | 1 | 0 | 0 | `scripts.file_lock` |
| `scripts.run_phase4_backtest` | 10 | 1 | 0 | 0 | `all_stock_dca.backtest` |
| `scripts.run_state_machine` | 333 | 0 | 1 | 0 |  |
| `scripts.run_ticker_maintenance` | 132 | 4 | 0 | 4 | `dca_paths`, `dca_ticker_paths`, `scripts.daily_per_ticker_calibration`, `scripts.run_tickers_daily_update` |
| `scripts.run_tickers_daily_update` | 538 | 4 | 1 | 1 | `dca_fileio`, `dca_paths`, `dca_ticker_paths`, `scripts.file_lock` |
| `scripts.same_day_pointer` | 1042 | 3 | 7 | 5 | `dca_fileio`, `dca_paths`, `scripts.run_classification` |
| `scripts.same_day_top_up_gate` | 719 | 3 | 4 | 94 | `dca_config`, `scripts.broker_reconciliation`, `scripts.reason_codes` |
| `scripts.schedule_watchdog` | 333 | 5 | 2 | 6 | `dca_fileio`, `dca_net_utils`, `dca_paths`, `near_close_dca`, `scripts.verify_schedule` |
| `scripts.scheduled_artifacts` | 1358 | 2 | 9 | 9 | `dca_fileio`, `scripts.same_day_pointer` |
| `scripts.scheduler_lease` | 465 | 1 | 4 | 2 | `scripts.file_lock` |
| `scripts.self_heal` | 122 | 0 | 0 | 2 |  |
| `scripts.sequestered_truth_comparator` | 900 | 0 | 2 | 0 |  |
| `scripts.shard_heartbeat` | 133 | 0 | 0 | 0 |  |
| `scripts.slot_attribution` | 36 | 3 | 1 | 1 | `dca_paths`, `scripts.run_classification`, `scripts.verify_schedule` |
| `scripts.snapshot_cycle` | 328 | 14 | 0 | 2 | `dca_config`, `dca_fileio`, `dca_fill_compare`, `dca_market_history`, `dca_market_sources`, `dca_paths`, `dca_rec_models`, `dca_rec_publish`, +6 more |
| `scripts.stream_candidates` | 1967 | 5 | 8 | 4 | `dca_config`, `dca_runner`, `morning_dca`, `near_close_dca`, `scripts.generate_buy_candidates` |
| `scripts.sync_live_prompts` | 228 | 1 | 0 | 0 | `scripts.check_live_prompt_drift` |
| `scripts.validate_native_placement` | 281 | 6 | 5 | 41 | `dca_config`, `dca_env`, `dca_fileio`, `dca_runner`, `near_close_dca`, `scripts.idempotency` |
| `scripts.verify_perplexity_pair` | 182 | 0 | 0 | 1 |  |
| `scripts.verify_schedule` | 355 | 6 | 8 | 22 | `dca_paths`, `dca_runner`, `scripts.finalize_run_evidence`, `scripts.same_day_pointer`, `scripts.scheduled_artifacts`, `scripts.slot_attribution` |
| `scripts.workflow_policy` | 230 | 0 | 0 | 3 |  |
| `scripts.workspace_probe` | 136 | 1 | 0 | 0 | `dca_paths` |
| `selfheal` | 5 | 4 | 0 | 0 | `selfheal.config`, `selfheal.notion_issue_loop`, `selfheal.reasons`, `selfheal.triage_reconcile` |
| `selfheal.config` | 291 | 0 | 2 | 1 |  |
| `selfheal.notion_issue_loop` | 1624 | 0 | 2 | 0 |  |
| `selfheal.reasons` | 185 | 0 | 2 | 0 |  |
| `selfheal.triage_reconcile` | 362 | 1 | 1 | 1 | `selfheal.notion_issue_loop` |
| `sizing_adjustment_reader` | 102 | 0 | 0 | 0 |  |
| `snap` | 46 | 0 | 0 | 0 |  |
