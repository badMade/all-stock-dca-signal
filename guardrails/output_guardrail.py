"""Output guardrail enforcing PII redaction and grounding/hallucination checks.

Layer 3 of the defense-in-depth architecture: inspects LLM-generated output before
it is returned to the user, redacting sensitive PII (emails, SSNs) and verifying
that output assertions are traceable to authorized retrieved context documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Pattern, Sequence, Set


@dataclass
class OutputCheckResult:
    """Detailed result from output guardrail processing."""

    content: str
    pii_redacted: bool
    hallucination_detected: bool
    redaction_count: int
    raw_content: str


class OutputGuardrail:
    """Sanitizes model outputs through regex PII redaction and context grounding verification."""

    DEFAULT_REDACTION_TEXT: str = "[REDACTED INFORMATION]"
    DEFAULT_FALLBACK_MESSAGE: str = (
        "I apologize, but I cannot verify this information against the authorized "
        "reference documents available to your role."
    )

    # Standard patterns for Email and SSN (both hyphenated 123-45-6789 and unhyphenated 9-digit)
    DEFAULT_EMAIL_PATTERN: str = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    DEFAULT_SSN_PATTERN: str = r"\b(?:\d{3}-\d{2}-\d{4})\b"

    def __init__(
        self,
        redaction_text: Optional[str] = None,
        fallback_message: Optional[str] = None,
        email_pattern: Optional[str] = None,
        ssn_pattern: Optional[str] = None,
        grounding_threshold: float = 0.5,
        min_words_for_grounding: int = 4,
    ) -> None:
        """Initialize the output guardrail with customizable patterns and thresholds.

        Args:
            redaction_text: Replacement text for detected PII. Defaults to '[REDACTED INFORMATION]'.
            fallback_message: Safe response returned when a hallucination is detected.
            email_pattern: Custom regex pattern for emails, or None for default.
            ssn_pattern: Custom regex pattern for SSNs, or None for default.
            grounding_threshold: Minimum fraction of key content words in an assertion that
                must be traceable to context (range 0.0 to 1.0).
            min_words_for_grounding: Minimum substantive word count for a sentence to be evaluated
                for hallucination (skips short conversational greetings like 'Hello').
        """
        self.redaction_text = redaction_text if redaction_text is not None else self.DEFAULT_REDACTION_TEXT
        self.fallback_message = fallback_message if fallback_message is not None else self.DEFAULT_FALLBACK_MESSAGE
        self.grounding_threshold = max(0.0, min(1.0, grounding_threshold))
        self.min_words_for_grounding = min_words_for_grounding

        email_regex = email_pattern if email_pattern is not None else self.DEFAULT_EMAIL_PATTERN
        ssn_regex = ssn_pattern if ssn_pattern is not None else self.DEFAULT_SSN_PATTERN

        self._email_compiled: Pattern[str] = re.compile(email_regex, re.IGNORECASE)
        self._ssn_compiled: Pattern[str] = re.compile(ssn_regex)

        # Common stop words excluded from hallucination lexical grounding
        self._stop_words: Set[str] = {
            "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "with",
            "about", "against", "between", "into", "through", "during", "before", "after",
            "above", "below", "from", "up", "down", "is", "are", "was", "were", "be",
            "been", "being", "have", "has", "had", "do", "does", "did", "can", "could",
            "shall", "should", "will", "would", "may", "might", "must", "it", "this",
            "that", "these", "those", "i", "you", "he", "she", "we", "they", "their",
            "his", "her", "its", "our", "your", "my",
            # RAG citation scaffolding and attribution words
            "based", "according", "document", "documents", "internal", "source", "reference",
            "records", "file", "stated", "mentioned", "summary", "information",
        }

    def redact_pii(self, text: str) -> tuple[str, int]:
        """Redact sensitive PII (emails and SSNs) from text using regular expressions.

        Args:
            text: Raw string to redact.

        Returns:
            Tuple of (redacted_text, total_redactions_made).
        """
        if not text:
            return text, 0

        redactions = 0

        # Redact SSNs
        text, ssn_count = self._ssn_compiled.subn(self.redaction_text, text)
        redactions += ssn_count

        # Redact Emails
        text, email_count = self._email_compiled.subn(self.redaction_text, text)
        redactions += email_count

        return text, redactions

    def check_hallucination(self, output_text: str, context_documents: Sequence[str]) -> bool:
        """Check if output_text makes assertions that cannot be traced to context.

        Splits output into sentences and verifies that factual content words in each
        informative sentence have grounding evidence in the combined context text.
        If any informative sentence falls below `grounding_threshold`, it is flagged
        as a hallucination.

        Args:
            output_text: Generated response text from model.
            context_documents: Sequence of authorized reference document contents.

        Returns:
            True if hallucination is detected (untraceable claim), False if grounded.
        """
        if not output_text.strip():
            return False

        # If no context documents were retrieved, any substantial factual assertion is untraceable
        context_corpus = " ".join(context_documents).lower()
        context_words = set(re.findall(r"\b[a-z0-9_]+\b", context_corpus))

        if not context_words:
            # Check if output contains substantive claims
            substantive_output = [
                w for w in re.findall(r"\b[a-z0-9_]+\b", output_text.lower())
                if w not in self._stop_words and len(w) > 2
            ]
            # If the model emits factual claims without any context, flag it
            return len(substantive_output) >= self.min_words_for_grounding

        # Split output into sentences
        sentences = [s.strip() for s in re.split(r"[.!?\n]+", output_text) if s.strip()]
        for sentence in sentences:
            tokens = [
                w for w in re.findall(r"\b[a-z0-9_]+\b", sentence.lower())
                if w not in self._stop_words and len(w) > 2
            ]
            if len(tokens) < self.min_words_for_grounding:
                continue

            grounded_tokens = [w for w in tokens if w in context_words]
            grounding_ratio = len(grounded_tokens) / len(tokens)

            if grounding_ratio < self.grounding_threshold:
                return True  # Untraceable claim detected

        return False

    def process(
        self,
        output_text: str,
        context_documents: Sequence[str],
    ) -> OutputCheckResult:
        """Execute full output guardrail pipeline: hallucination check then PII redaction.

        Args:
            output_text: Raw generation output from LLM.
            context_documents: Sequence of authorized document strings used in context.

        Returns:
            OutputCheckResult with final text (or fallback) and redaction metrics.
        """
        is_hallucination = self.check_hallucination(output_text, context_documents)
        if is_hallucination:
            # Fall back to safe response
            redacted_fallback, _ = self.redact_pii(self.fallback_message)
            return OutputCheckResult(
                content=redacted_fallback,
                pii_redacted=False,
                hallucination_detected=True,
                redaction_count=0,
                raw_content=output_text,
            )

        # Redact any PII present in verified response
        redacted_text, count = self.redact_pii(output_text)
        return OutputCheckResult(
            content=redacted_text,
            pii_redacted=count > 0,
            hallucination_detected=False,
            redaction_count=count,
            raw_content=output_text,
        )
