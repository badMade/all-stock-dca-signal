# Architecture Decision Record — Future Supported-Architecture Fine-Tuning Project

Status: deferred (intent documented only, no implementation planned yet)
Date: 2026-09-19

## 1) Context

`all-stock-dca`'s existing ML components (Ridge regression forecaster, Wondrous
Octopus custom causal Transformer) are not — and are not intended to become —
AirLLM-compatible. Wondrous Octopus is a bespoke numeric-forecasting
architecture; AirLLM only knows how to layer-split specific known HF text-LLM
architectures (Llama, Qwen, Mistral, ChatGLM, DeepSeek, Gemma, etc.).

A separate need exists for a future project: fine-tune a text-based LLM using
a raw PyTorch training loop, and be able to use AirLLM afterward for
post-training serving (cheap deployment) and post-training smoke
evaluation. This ADR exists solely to record that intent and the one
constraint that has to be honored from day one of that project, so the
option isn't accidentally closed off before the project starts.

AirLLM is not the right tool for evaluation *during* training: it executes
sequential layer-by-layer offloading to and from CPU/disk on each forward
pass, which is slow (often 1-3 tokens/sec or worse, depending on storage
throughput). Mid-training checkpoint evaluation should use standard
HuggingFace/PyTorch inference on a validation subset with the training
process's own VRAM, not AirLLM — reserve AirLLM for the post-training
smoke-eval and serving steps in Section 6, where its low-VRAM trade-off
against latency is the point.

## 2) Decision (deferred, but binding once the project starts)

When this project begins:

1. **Base model must be one of AirLLM's supported architectures.** Confirm
   against the current supported list at build time (it changes — check
   `lyogavin/airllm` README before committing to a base model), not against
   this ADR's list, which will go stale. As of this writing: Llama family,
   Qwen family, Mistral, ChatGLM, DeepSeek V2/V3, Gemma, Phi-4.
2. **The checkpoint handed to AirLLM must be a plain `transformers.PreTrainedModel`
   save, not a PEFT wrapper**, even with a custom PyTorch loop instead of
   `Trainer`. This is the load-bearing constraint. Concretely:
   - Load the base model via `AutoModelForCausalLM.from_pretrained(...)`.
   - Do not extract or reconstruct the raw `nn.Module` weights into a custom
     wrapper class. Full fine-tune keeps the top-level object a
     `PreTrainedModel` throughout training. PEFT/LoRA does not — training
     wraps the base model in a `PeftModel`, so the invariant applies to the
     saved artifact, not the in-training object: call
     `model.merge_and_unload()` (or equivalent) before
     `model.save_pretrained(path)`, so what lands on disk is a standard
     `PreTrainedModel` checkpoint. AirLLM has no adapter-loading path of its
     own (see Section 4), so an un-merged adapter checkpoint will not load.
   - Custom training loop is fine (optimizer, scheduler, loss, logging all
     yours) — what can't change is the saved checkpoint's model class.
3. Checkpoints saved via `model.save_pretrained(path)` produce standard
   `config.json` + `safetensors` output, which AirLLM's `AutoModel` can load
   directly, no conversion step. `tokenizer.save_pretrained(path)` must be
   called alongside it — instruct-tuned checkpoints (Section 3) depend on the
   tokenizer config and chat template living next to the weights, and AirLLM
   does not reconstruct those from the model files alone.

## 3) Candidate use case (motivating, not committed)

Offload part of the existing CI-log-driven PR-fix workflow: instead of
pinging a bot (`@copilot`, as done today in `github-pr-check-fixer` /
`copilot-fix-failing-checks` / `github-pr-review-comment-fixer`) to act on
failing CI output or review comments, a fine-tuned local model would read
captured logs and generate the fix content/prompt directly.

This has one concrete implication for Section 2: the task is instruction-
following (log/error text in, fix prompt or patch text out), not open-ended
generation. Base model selection at project start should default to an
**instruct-tuned** checkpoint (e.g. Llama-3-Instruct, Qwen-Instruct,
DeepSeek-Coder-Instruct) rather than a base pretrain-only model, still
subject to the AirLLM-supported-architecture constraint in Section 2.

This use case is not committed — it's recorded here because it's the
concrete scenario that prompted this ADR, and it usefully narrows "which
kind of supported model" without narrowing which one.

Cost motivation: the current workflow pays per-invocation for a hosted
coding-assistant bot (`@copilot`) on every failing-CI/log-fix cycle. A
self-hosted fine-tuned model, served via AirLLM on cheap hardware, replaces
that recurring per-call cost with a one-time training cost plus fixed
serving infra — the trade-off to validate at project start is whether
self-hosted output quality is good enough to replace the bot calls it's
meant to reduce.

## 4) What this ADR is explicitly not deciding

- Which specific base model/size (deferred — pick at project start based on
  task and available compute)
- Full fine-tune vs. LoRA/QLoRA (deferred — either is compatible with the
  constraint in Section 2, as long as adapters are merged before AirLLM
  loads the checkpoint; AirLLM has no adapter-loading path of its own)
- Whether the log-fix use case above is actually what gets built, versus
  some other text-LLM task
- Timeline (no commitment to when this project starts)

## 5) Consequences

- Locks base-model choice to AirLLM's supported list, which is a real
  constraint if a better base model outside that list emerges later. Judged
  acceptable since the list already covers the major open-weight LLM
  families.
- Requires discipline to not "simplify" the training loop by stripping the
  HF model wrapper — a natural refactoring temptation that would silently
  break AirLLM compatibility without erroring at training time (it would
  only surface later, when AirLLM tries to load the checkpoint and fails).
- No cost paid now — this is a documentation-only decision. No dependencies
  introduced, nothing built (YAGNI-compliant).

## 6) Scaffold (structure only, not implemented)

```text
future-llm-finetune-project/
├── README.md                  # states the AirLLM-compatibility constraint up front
├── train.py                   # custom PyTorch loop; wraps AutoModelForCausalLM
├── config/
│   └── base_model.yaml        # pins base model repo id; must be AirLLM-supported
├── checkpoints/                # model.save_pretrained() + tokenizer.save_pretrained()
│                                # output lands here
├── eval/
│   └── airllm_eval.py         # post-training smoke eval only (see Section 1) -
│                               # loads latest checkpoint via AirLLM, runs fixed
│                               # prompt set, logs outputs
└── serve/
    └── airllm_serve.py        # thin FastAPI wrapper, AutoModel.from_pretrained,
                                # compression='4bit' for deployed latency
```

Nothing under this tree needs to be created now. This layout is the intended
shape when the project starts, so the constraint in Section 2 is structurally
enforced (train.py has nowhere else to put the model object except through
the HF wrapper) rather than relying on remembering a rule.

## 7) Security considerations (model-agnostic)

These apply identically regardless of which AirLLM-supported architecture
is chosen — the security boundary is the interface design, not the model:

- **Suggest-only, mirroring the project's existing fail-closed principle.**
  Same rule as `SizingSuggester` in `dca-sizing-signals`: this component
  never auto-commits, auto-merges, or auto-executes a fix. Output is a
  draft/prompt for human review, full stop. This is what actually prevents
  a bad model output from becoming a bad commit — not model selection.
- **Secrets scrubbing on logs, both directions.** CI/failure logs routinely
  contain credentials, tokens, or internal URLs. These must be scrubbed
  before (a) inclusion in training data and (b) submission to the model at
  inference time. This is the existing secrets-guard discipline applied to
  a new data path, not a new mechanism.
- **Local-only serving reduces exposure vs. today's baseline, but only if the
  serve layer is actually bound local-only.** The Section 6 FastAPI wrapper
  must bind to loopback (`127.0.0.1`) or otherwise sit behind an
  authentication boundary — a wrapper listening on `0.0.0.0` with no auth
  would ship log content to any host that can reach the port, which is worse
  than today's baseline, not better. With that binding in place, logs never
  leave the machine — smaller exfiltration surface than the current
  workflow, which sends log content to a third-party cloud bot (`@copilot`).
- **Weight provenance, not model choice, is the actual variable.** Whichever
  architecture is picked, download weights from the official HF repo (not a
  mirror) and verify checksums — the same diligence already applied to
  vetting the `airllm` package itself. This applies equally to all
  candidates; none of Llama/Qwen/Mistral/ChatGLM/DeepSeek is inherently
  safer or riskier here since all run identically through local inference.
- **License is a legal axis, not a security one, but worth tracking
  alongside:** Llama models are gated on HF (must accept Meta's license per
  model); Qwen and DeepSeek are permissively licensed with no gate.

## 8) Trigger to revisit

Start this project when there's an actual task needing a fine-tuned text LLM.
Re-check AirLLM's supported-architecture list at that time — do not assume
the list above is still current.
