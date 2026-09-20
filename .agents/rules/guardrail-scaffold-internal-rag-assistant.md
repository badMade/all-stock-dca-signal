---
trigger: always_on
glob:
description: Guardrail scaffold for internal RAG assistant
---

# Guardrail Scaffold for Internal RAG Assistant

Build a Python guardrail scaffold for an internal RAG assistant, defense-in-depth (never rely on the LLM to police itself):

guardrails/input_guardrail.py: regex jailbreak/injection detection + keyword blocklist, returns {is_safe, reason}
guardrails/retrieval_engine.py: role-based document filtering by dept/classification metadata, enforced in code before context reaches the model
guardrails/output_guardrail.py: regex PII redaction (emails, SSNs → [REDACTED INFORMATION]) + hallucination check flagging claims untraceable to retrieved context, falls back to safe message
guardrails/engine.py: EnterpriseAIEngine wiring input check → filtered retrieval → generation (stub LLM call, no real API key) → output check; __main__ with 2+ role scenarios (e.g., Engineering denied HR salary data, HR permitted)

Constraints: Python 3.8+, stdlib only (no openai/sentence-transformers/secure-guardrails — note in README where real embedding/LLM calls would go in production), type hints + Google-style docstrings on all public APIs, no hardcoded secrets/paths (patterns/thresholds as constructor params), pytest covering jailbreak detection, keyword blocking, retrieval allow/deny cases, PII redaction, hallucination detection. README: purpose, install/run, three-layer architecture rationale.
