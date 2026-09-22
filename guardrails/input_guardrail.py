"""Input guardrail for detecting prompt injections, jailbreaks, and prohibited keywords.

Layer 1 of the defense-in-depth architecture: inspects incoming user prompts
before any document retrieval or model generation occurs.
"""

from __future__ import annotations

import re
from typing import List, Optional, Pattern, Sequence, Set


class InputCheckResult(dict):
    """Result of an input guardrail evaluation.

    Inherits from dict to provide direct dictionary access (e.g. result['is_safe'])
    while also exposing convenient property accessors.
    """

    def __init__(self, is_safe: bool, reason: Optional[str] = None) -> None:
        """Initialize the result object.

        Args:
            is_safe: Whether the input passed all safety checks.
            reason: Explanation if input was flagged as unsafe, otherwise None.
        """
        super().__init__(is_safe=is_safe, reason=reason)

    @property
    def is_safe(self) -> bool:
        """Return whether the input is safe."""
        return bool(self["is_safe"])

    @property
    def reason(self) -> Optional[str]:
        """Return the reason why the input was flagged, or None."""
        return self["reason"]


class InputGuardrail:
    """Detects jailbreak attempts, prompt injections, and prohibited keywords.

    Enforces pre-execution validation on user input prompts using configurable
    regular expression heuristics and exact-match keyword blocklists.
    """

    DEFAULT_JAILBREAK_PATTERNS: Sequence[str] = (
        r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:(?:all|previous|prior|above|system)\s+)*(?:instructions|prompts?|rules?|directives?)",
        r"(?i)\b(?:reveal|show|display|leak|print|dump)\s+(?:the\s+|your\s+)?(?:system\s+prompt|initial\s+prompt|developer\s+mode|internal\s+rules|prompt)\b",
        r"(?i)\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(?:an?\s+)?(?:unfiltered|unrestricted|evil|jailbreak|DAN|Do\s+Anything\s+Now)",
        r"(?i)\b(?:developer\s+mode|god\s+mode|jailbreak\s+mode)\s+(?:is\s+)?(?:enabled|active|on)",
        r"(?i)\b(?:bypass|disable|circumvent)\s+(?:the\s+)?(?:safety|guardrails?|filters?|restrictions?)",
        r"(?i)\boutput\s+the\s+exact\s+text\s+above\b",
    )

    DEFAULT_BLOCKED_KEYWORDS: Sequence[str] = (
        "rm -rf",
        "drop table",
        "delete from",
        "eval(",
        "exec(",
        "/etc/passwd",
        "/etc/shadow",
        "nc -e",
        "bash -i",
    )

    def __init__(
        self,
        jailbreak_patterns: Optional[Sequence[str]] = None,
        blocked_keywords: Optional[Sequence[str]] = None,
        case_sensitive_keywords: bool = False,
    ) -> None:
        """Initialize the InputGuardrail with customizable patterns and keywords.

        Args:
            jailbreak_patterns: Sequence of regex patterns matching injection/jailbreak attempts.
                Defaults to DEFAULT_JAILBREAK_PATTERNS if None.
            blocked_keywords: Sequence of prohibited keywords/phrases.
                Defaults to DEFAULT_BLOCKED_KEYWORDS if None.
            case_sensitive_keywords: Whether keyword matching should be case-sensitive.
        """
        raw_patterns = jailbreak_patterns if jailbreak_patterns is not None else self.DEFAULT_JAILBREAK_PATTERNS
        self._compiled_patterns: List[Pattern[str]] = [re.compile(p) for p in raw_patterns]

        raw_keywords = blocked_keywords if blocked_keywords is not None else self.DEFAULT_BLOCKED_KEYWORDS
        self._case_sensitive_keywords = case_sensitive_keywords
        if self._case_sensitive_keywords:
            self._blocked_keywords: Set[str] = set(raw_keywords)
        else:
            self._blocked_keywords = {kw.lower() for kw in raw_keywords}

    def validate(self, text: str) -> InputCheckResult:
        """Inspect the input text against jailbreak patterns and blocked keywords.

        Args:
            text: Raw input query or prompt string to evaluate.

        Returns:
            InputCheckResult containing 'is_safe' (bool) and 'reason' (Optional[str]).
        """
        if not text or not isinstance(text, str):
            return InputCheckResult(is_safe=True, reason=None)

        # 1. Regex jailbreak and prompt injection detection
        for pattern in self._compiled_patterns:
            match = pattern.search(text)
            if match:
                matched_snippet = match.group(0)[:60]
                return InputCheckResult(
                    is_safe=False,
                    reason=f"Jailbreak or prompt injection detected: {matched_snippet!r}",
                )

        # 2. Blocked keyword detection
        comparison_text = text if self._case_sensitive_keywords else text.lower()
        for kw in self._blocked_keywords:
            if kw in comparison_text:
                return InputCheckResult(
                    is_safe=False,
                    reason=f"Prohibited keyword detected: {kw!r}",
                )

        return InputCheckResult(is_safe=True, reason=None)
