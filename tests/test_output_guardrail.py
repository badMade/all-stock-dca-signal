import pytest

from guardrails.output_guardrail import OutputGuardrail


class TestOutputGuardrail:
    """Test suite for OutputGuardrail PII redaction and context hallucination checks."""

    @pytest.fixture
    def guardrail(self) -> OutputGuardrail:
        return OutputGuardrail()

    # --- Original Base Tests ---

    def test_email_redaction(self, guardrail: OutputGuardrail):
        text = "Please reach out to john.doe@enterprise.corp for assistance."
        redacted, count = guardrail.redact_pii(text)
        assert count == 1
        assert redacted == "Please reach out to [REDACTED INFORMATION] for assistance."

    def test_ssn_redaction(self, guardrail: OutputGuardrail):
        text = "Employee tax identifier recorded as 123-45-6789 on Form W-2."
        redacted, count = guardrail.redact_pii(text)
        assert count == 1
        assert redacted == "Employee tax identifier recorded as [REDACTED INFORMATION] on Form W-2."

    def test_multiple_pii_redaction(self, guardrail: OutputGuardrail):
        text = "Contact alice@acme.com or SSN 999-00-1111."
        redacted, count = guardrail.redact_pii(text)
        assert count == 2
        assert "[REDACTED INFORMATION]" in redacted
        assert "alice@acme.com" not in redacted
        assert "999-00-1111" not in redacted

    def test_grounded_output_passes_hallucination_check(self, guardrail: OutputGuardrail):
        context = [
            "Executive salaries range from $250k to $400k in the HR salary schedule."
        ]
        grounded_output = "The executive salaries range from $250k to $400k according to schedule."
        assert guardrail.check_hallucination(grounded_output, context) is False

    def test_hallucinated_claim_triggers_fallback(self, guardrail: OutputGuardrail):
        context = [
            "Company holidays include Memorial Day and Thanksgiving."
        ]
        hallucinated_output = (
            "We discovered supersonic alien spacecraft landing in Antarctica during winter."
        )
        result = guardrail.process(hallucinated_output, context)
        assert result.hallucination_detected is True
        assert result.content == guardrail.fallback_message

    # --- New Tests: Threshold Boundaries ---

    def test_threshold_boundary_below(self):
        # 4 out of 10 informative words grounded (ratio 0.40) -> fails with threshold 0.50
        guardrail = OutputGuardrail(grounding_threshold=0.50)
        context = ["apples bananas cherries dates"]
        # Informative words (length > 2, not in stop words):
        # apples, bananas, cherries, dates, elephants, falcons, giraffes, hippos, iguanas, jaguars
        output = "Apples bananas cherries dates elephants falcons giraffes hippos iguanas jaguars are amazing."
        assert guardrail.check_hallucination(output, context) is True

    def test_threshold_boundary_exact(self):
        # 5 out of 10 informative words grounded (ratio 0.50) -> passes with threshold 0.50
        guardrail = OutputGuardrail(grounding_threshold=0.50)
        context = ["apples bananas cherries dates elephants"]
        # Informative words:
        # apples, bananas, cherries, dates, elephants, falcons, giraffes, hippos, iguanas, jaguars
        # Use exactly 10 non-stop words (Apples...jaguars)
        output = "Apples bananas cherries dates elephants falcons giraffes hippos iguanas jaguars are in."
        assert guardrail.check_hallucination(output, context) is False

    def test_threshold_boundary_above(self):
        # 6 out of 10 informative words grounded (ratio 0.60) -> passes with threshold 0.50
        guardrail = OutputGuardrail(grounding_threshold=0.50)
        context = ["apples bananas cherries dates elephants falcons"]
        # Informative words:
        # apples, bananas, cherries, dates, elephants, falcons, giraffes, hippos, iguanas, jaguars
        # Use exactly 10 non-stop words (Apples...jaguars)
        output = "Apples bananas cherries dates elephants falcons giraffes hippos iguanas jaguars are in."
        assert guardrail.check_hallucination(output, context) is False

    # --- New Tests: Type and Null Violations ---

    def test_type_violation_none(self, guardrail: OutputGuardrail):
        with pytest.raises(TypeError):
            guardrail.redact_pii(None)  # type: ignore
        with pytest.raises(TypeError):
            guardrail.redact_pii(123)  # type: ignore
        with pytest.raises(TypeError):
            guardrail.redact_pii(["list"])  # type: ignore

        with pytest.raises(TypeError):
            guardrail.check_hallucination(None, ["context"])  # type: ignore
        with pytest.raises(TypeError):
            guardrail.check_hallucination(123, ["context"])  # type: ignore

        with pytest.raises(TypeError):
            guardrail.process(None, ["context"])  # type: ignore
        with pytest.raises(TypeError):
            guardrail.process(123, ["context"])  # type: ignore

    def test_null_violation_empty_string(self, guardrail: OutputGuardrail):
        # Empty string should be handled gracefully and return the empty string
        redacted, count = guardrail.redact_pii("")
        assert redacted == ""
        assert count == 0

        # Empty string hallucination check should return False
        assert guardrail.check_hallucination("", ["context"]) is False

        # Process should handle empty string
        result = guardrail.process("", ["context"])
        assert result.content == ""
        assert result.hallucination_detected is False
        assert result.pii_redacted is False
        assert result.redaction_count == 0

    # --- New Tests: Execution Bypasses ---

    def test_conversational_greetings_bypassed(self, guardrail: OutputGuardrail):
        context = ["Heavy informative text goes here regarding quarterly earnings."]
        # Less than min_words_for_grounding informative words (default min is 4)
        greeting = "Hello there! How can I help?"
        assert guardrail.check_hallucination(greeting, context) is False

        # Another short phrase with few substantive words
        short_phrase = "Yes, absolutely."
        assert guardrail.check_hallucination(short_phrase, context) is False

        # Process should pass
        result = guardrail.process(greeting, context)
        assert result.hallucination_detected is False
        assert result.content == greeting

    # --- New Tests: Custom Configurations ---

    def test_custom_configurations(self):
        # Custom PII redaction text and custom regex for secret keys (e.g. "SECRET-XXX")
        custom_redaction = "<CENSORED>"
        custom_fallback = "I can't answer that safely."
        custom_ssn_pattern = r"\bSECRET-\w{3}\b" # Override SSN to act as a secret key blocker

        guardrail = OutputGuardrail(
            redaction_text=custom_redaction,
            fallback_message=custom_fallback,
            ssn_pattern=custom_ssn_pattern
        )

        # Test custom redaction triggers
        text = "My key is SECRET-ABC and email is test@example.com."
        redacted, count = guardrail.redact_pii(text)
        assert count == 2
        assert "<CENSORED>" in redacted
        assert "SECRET-ABC" not in redacted
        assert "test@example.com" not in redacted

        # Test custom fallback triggers on hallucination
        result = guardrail.process("A massive long false claim about entirely unrelated matters causing hallucination detection.", ["Small context."])
        assert result.hallucination_detected is True
        assert result.content == custom_fallback
