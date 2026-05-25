"""Unit tests for alert and result Pydantic models."""

import pytest
from pydantic import ValidationError

from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource
from alerttriage.src.core.result_models import AnalysisResult, Verdict


def make_alert(**kwargs) -> Alert:
    defaults = {
        "client_id": "test-client",
        "source": AlertSource.MANUAL,
        "rule_name": "Test Rule",
        "severity": AlertSeverity.HIGH,
        "title": "Suspicious login",
        "description": "Multiple failed logins followed by success.",
    }
    return Alert(**{**defaults, **kwargs})


class TestAlert:
    def test_creates_with_defaults(self):
        a = make_alert()
        assert a.id
        assert a.client_id == "test-client"
        assert a.severity == AlertSeverity.HIGH

    def test_client_id_stripped_and_lowercased(self):
        a = make_alert(client_id="  Acme-Corp  ")
        assert a.client_id == "acme-corp"

    def test_empty_client_id_raises(self):
        with pytest.raises(ValidationError):
            make_alert(client_id="   ")

    def test_tags_default_empty(self):
        a = make_alert()
        assert a.tags == []


class TestAnalysisResult:
    def test_total_tokens(self):
        r = AnalysisResult(
            alert_id="a1",
            client_id="c1",
            model_id="claude-sonnet",
            verdict=Verdict.TRUE_POSITIVE,
            confidence=0.9,
            summary="Confirmed TP",
            reasoning="Multiple indicators.",
            prompt_tokens=800,
            completion_tokens=200,
        )
        assert r.total_tokens == 1000

    def test_confidence_clamped(self):
        with pytest.raises(ValidationError):
            AnalysisResult(
                alert_id="a1",
                client_id="c1",
                model_id="m",
                verdict=Verdict.UNKNOWN,
                confidence=1.5,
                summary="",
                reasoning="",
            )
