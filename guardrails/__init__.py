"""Guardrail Scaffold for Internal RAG Assistant.

Provides three-layer defense-in-depth security:
  - Layer 1: Input guardrail for jailbreak & keyword injection detection.
  - Layer 2: Retrieval engine with code-enforced role-based access control.
  - Layer 3: Output guardrail for PII redaction and context grounding/hallucination checks.
"""

from __future__ import annotations

from guardrails.engine import EngineResponse, EnterpriseAIEngine
from guardrails.input_guardrail import InputCheckResult, InputGuardrail
from guardrails.output_guardrail import OutputGuardrail
from guardrails.retrieval_engine import Document, RetrievalEngine, UserContext

__all__ = [
    "Document",
    "EngineResponse",
    "EnterpriseAIEngine",
    "InputCheckResult",
    "InputGuardrail",
    "OutputGuardrail",
    "RetrievalEngine",
    "UserContext",
]
