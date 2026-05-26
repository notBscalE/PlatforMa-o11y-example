"""Tests for Pydantic models and pure-logic helpers."""

import pytest
from pydantic import ValidationError

# conftest sets env vars before this import
from agents.diagnostic import DiagnosisResult


def _valid_diagnosis(**overrides) -> dict:
    base = dict(
        root_cause="Memory limit exceeded on pod",
        category="infrastructure",
        severity="high",
        affected_components=["platformma-app"],
        proposed_fix="Increase memory limits in Helm values",
        proposed_fix_details={"files_to_change": []},
        immediate_actions=["kubectl rollout restart deployment/platformma -n platformma"],
        evidence=["OOMKilled event on pod platformma-xxx"],
        can_auto_remediate=True,
    )
    base.update(overrides)
    return base


def test_diagnosis_result_valid():
    d = DiagnosisResult(**_valid_diagnosis())
    assert d.category == "infrastructure"
    assert d.severity == "high"
    assert d.can_auto_remediate is True


def test_diagnosis_result_all_categories():
    for cat in ("infrastructure", "code", "transient", "unknown"):
        d = DiagnosisResult(**_valid_diagnosis(category=cat))
        assert d.category == cat


def test_diagnosis_result_all_severities():
    for sev in ("critical", "high", "medium", "low"):
        d = DiagnosisResult(**_valid_diagnosis(severity=sev))
        assert d.severity == sev


def test_diagnosis_result_invalid_category():
    with pytest.raises(ValidationError):
        DiagnosisResult(**_valid_diagnosis(category="networking"))


def test_diagnosis_result_invalid_severity():
    with pytest.raises(ValidationError):
        DiagnosisResult(**_valid_diagnosis(severity="urgent"))


def test_diagnosis_result_empty_lists_allowed():
    d = DiagnosisResult(**_valid_diagnosis(immediate_actions=[], evidence=[]))
    assert d.immediate_actions == []
    assert d.evidence == []
