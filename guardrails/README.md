# Guardrail Scaffold for Internal RAG Assistant

A Python standard-library defense-in-depth security scaffold for internal enterprise Retrieval-Augmented Generation (RAG) assistants.

## Purpose

Internal enterprise AI assistants interact with proprietary company data across multiple departments (Engineering, HR, Finance, Legal). Relying on the Large Language Model (LLM) to enforce access control or police its own outputs violates the fundamental security principle of **defense-in-depth**.

This scaffold enforces strict security boundaries **outside** the LLM:
1. **Layer 1 (Pre-Execution):** Input Guardrail inspecting user prompts for prompt injections, jailbreaks, and banned shell/SQL keywords before any retrieval occurs.
2. **Layer 2 (Context Boundary):** Retrieval Engine enforcing Role-Based Access Control (RBAC) and security clearance metadata in code before any document context can reach the model prompt.
3. **Layer 3 (Post-Execution):** Output Guardrail performing regex-based PII redaction (masking email addresses and SSNs to `[REDACTED INFORMATION]`) and asserting context-grounding to flag hallucinations and trigger safe fallback responses.

---

## Three-Layer Defense-in-Depth Architecture Rationale

```
[ User Query + User Context (Role, Dept, Clearance) ]
                          │
                          ▼
            ┌───────────────────────────┐
            │   LAYER 1: INPUT GUARD    │  ──► [UNSAFE: Block Prompt Injection / Banned Terms]
            └───────────────────────────┘
                          │ (Safe)
                          ▼
            ┌───────────────────────────┐
            │  LAYER 2: RBAC RETRIEVAL  │  ──► [FILTERED IN CODE: Non-authorized docs pruned]
            └───────────────────────────┘
                          │ (Authorized Docs Only)
                          ▼
            ┌───────────────────────────┐
            │    MODEL INFERENCE (LLM)  │  ──► (Production: OpenAI / Anthropic / Local LLM)
            └───────────────────────────┘
                          │ (Raw Response)
                          ▼
            ┌───────────────────────────┐
            │   LAYER 3: OUTPUT GUARD   │
            │  - Context Grounding Check│  ──► [UNGROUNDED: Fall back to safe message]
            │  - Regex PII Redaction    │  ──► [SSN / Email -> [REDACTED INFORMATION]]
            └───────────────────────────┘
                          │
                          ▼
            [ Sanitized, Audited Enterprise Response ]
```

### Why the LLM Must Never Police Itself

- **Prompt Injections & Jailbreaks:** System prompt directives like *"Never reveal confidential HR data"* can be overridden by sophisticated jailbreak vectors ("DAN mode", hypothetical roleplay, unicode obfuscation). Input filtering at Layer 1 prevents adversarial prompts from reaching downstream systems.
- **Context Bleed (The Ingestion Trap):** If confidential salary documents are placed in an LLM's prompt context, the model may inadvertently disclose or reference them even when instructed not to. Enforcing department and clearance boundaries in code (Layer 2) ensures unauthorized context never reaches the prompt token window.
- **Inadvertent PII Leakage:** Internal training corpora and documents frequently contain employee emails, phone numbers, and Social Security Numbers. Deterministic regular expressions at Layer 3 guarantee redaction regardless of model behavior.
- **Hallucination & Fabrication:** Without context-grounding checks, models may extrapolate confidently beyond reference data. Verifying substantive claims against authorized source context protects organizational decision-making.

---

## Installation & Running

The scaffold is built exclusively with Python 3.8+ standard library (no external runtime dependencies required).

### Run the Interactive Multi-Scenario Demo
```bash
python3 guardrails/engine.py
```

### Run the Test Suite
```bash
python3 -m pytest tests/test_guardrails.py -v
```

---

## Production Integration Points

In this repository, the core scaffold uses pure standard-library primitives (TF-IDF token matching, deterministic stub generation) to remain portable, lightweight, and independent of external API keys.

In production environments, replace the stubbed components as follows:

| Component | Standard Library Implementation | Production Drop-In Replacement |
| :--- | :--- | :--- |
| **Embeddings & Vector Search** | `RetrievalEngine.retrieve()` token overlap | Vector Database (e.g., Pinecone, Qdrant, Chroma, pgvector) with metadata filtering: `filter={"dept": user.dept, "clearance": {"$lte": user.clearance}}` |
| **Embedding Models** | Lexical tokenization | `sentence-transformers/all-MiniLM-L6-v2` or `text-embedding-3-small` |
| **LLM Inference** | `default_stub_llm()` | OpenAI (`gpt-4o`), Anthropic (`claude-3-5-sonnet`), or local vLLM (`meta-llama/Llama-3-70B`) via the `llm_client` callable in `EnterpriseAIEngine` |
| **Grounding Verification** | `OutputGuardrail.check_hallucination()` lexical overlap | NLI classifier (e.g., DeBERTa-v3-base-mnli) or LLM-judge verification agent |

---

## API Summary

- `guardrails.InputGuardrail(jailbreak_patterns=..., blocked_keywords=...)`: Validates user prompt queries.
- `guardrails.RetrievalEngine(documents=..., clearance_hierarchy=...)`: Filters documents by department, role, and clearance rank before model inference.
- `guardrails.OutputGuardrail(redaction_text=..., grounding_threshold=...)`: Redacts sensitive PII and verifies context grounding.
- `guardrails.EnterpriseAIEngine(input_guardrail=..., retrieval_engine=..., output_guardrail=..., llm_client=...)`: Orchestrates the full defense-in-depth pipeline.
