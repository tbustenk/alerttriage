"""Unit tests for the PII anonymisation layer."""

from __future__ import annotations

from alerttriage.src.anonymize import Anonymizer
from alerttriage.src.core.alert_models import Alert, AlertContext, AlertSeverity, AlertSource


class _Cfg:
    anonymize_salt = "test-salt"


def _alert(**kw) -> Alert:
    defaults = {
        "client_id": "acme",
        "source": AlertSource.MANUAL,
        "rule_name": "Brute Force",
        "severity": AlertSeverity.HIGH,
        "title": "login failures",
        "description": "user bob@acme.com from 10.0.0.5 failed login 50 times",
        "context": AlertContext(
            host_info={"ip": "10.0.0.5", "hostname": "win-srv-01"},
            user_info={"username": "bob"},
        ),
        "raw_payload": {
            "card": "4111-1111-1111-1111",
            "ssn": "123-45-6789",
            "key": "sk-AAAAAAAAAAAAAAAAAAAAAAAA",
        },
    }
    defaults.update(kw)
    return Alert(**defaults)


def test_anonymizes_ip_username_hostname() -> None:
    a = _alert()
    anon, _ = Anonymizer(_Cfg()).anonymize(a)
    assert anon.context.host_info["ip"].startswith("<IP:")
    assert anon.context.host_info["hostname"].startswith("<HOST:")
    assert anon.context.user_info["username"].startswith("<USER:")


def test_anonymizes_credit_card_ssn_api_key_in_payload() -> None:
    a = _alert()
    anon, _ = Anonymizer(_Cfg()).anonymize(a)
    assert "<CREDIT_CARD" in anon.raw_payload["card"]
    assert "<SSN" in anon.raw_payload["ssn"]
    assert "<API_KEY" in anon.raw_payload["key"]


def test_anonymizes_email_in_description() -> None:
    a = _alert()
    anon, _ = Anonymizer(_Cfg()).anonymize(a)
    assert "<EMAIL" in anon.description
    assert "bob@acme.com" not in anon.description


def test_reverse_map_round_trips_values() -> None:
    a = _alert()
    anon, reverse_map = Anonymizer(_Cfg()).anonymize(a)
    # Every token in the anonymised description must be in the reverse map.
    ip_token = anon.context.host_info["ip"]
    assert reverse_map[ip_token] == "10.0.0.5"


def test_same_value_yields_same_token() -> None:
    a = _alert()
    anon1, _ = Anonymizer(_Cfg()).anonymize(a)
    anon2, _ = Anonymizer(_Cfg()).anonymize(a)
    assert anon1.context.host_info["ip"] == anon2.context.host_info["ip"]


def test_different_salts_produce_different_tokens() -> None:
    class _CfgA:
        anonymize_salt = "salt-a"

    class _CfgB:
        anonymize_salt = "salt-b"

    a = _alert()
    anon_a, _ = Anonymizer(_CfgA()).anonymize(a)
    anon_b, _ = Anonymizer(_CfgB()).anonymize(a)
    assert anon_a.context.host_info["ip"] != anon_b.context.host_info["ip"]


def test_empty_context_does_not_crash() -> None:
    a = _alert(context=AlertContext(), raw_payload={})
    anon, _ = Anonymizer(_Cfg()).anonymize(a)
    assert anon.id == a.id
