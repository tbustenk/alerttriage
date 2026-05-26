"""Extended tests for secrets manager — mocked AWS SM and Vault backends."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from alerttriage.src.secrets.manager import (
    clear_cache,
    get_secret,
    prefetch,
    _get_from_aws,
    _get_from_vault,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


class TestGetFromAWS:
    def test_success(self):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"MY_KEY": "secret-value"})
        }
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            val = _get_from_aws("my-secret-id", "MY_KEY")

        assert val == "secret-value"

    def test_key_not_in_secret(self):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps({"OTHER": "x"})}
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            val = _get_from_aws("my-secret-id", "MISSING_KEY")

        assert val is None

    def test_boto3_not_installed(self):
        with patch.dict("sys.modules", {"boto3": None}):
            val = _get_from_aws("my-secret-id", "ANY_KEY")
        assert val is None

    def test_boto3_exception(self):
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("connection refused")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            val = _get_from_aws("my-secret-id", "ANY_KEY")

        assert val is None


class TestGetFromVault:
    def test_success(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "root-token")

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"MY_VAULT_KEY": "vault-secret"}}
        }
        mock_hvac = MagicMock()
        mock_hvac.Client.return_value = mock_client

        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            val = _get_from_vault("MY_VAULT_KEY")

        assert val == "vault-secret"

    def test_key_missing_from_vault(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"OTHER_KEY": "x"}}
        }
        mock_hvac = MagicMock()
        mock_hvac.Client.return_value = mock_client

        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            val = _get_from_vault("MISSING_KEY")

        assert val is None

    def test_hvac_not_installed(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        with patch.dict("sys.modules", {"hvac": None}):
            val = _get_from_vault("ANY_KEY")
        assert val is None

    def test_vault_exception(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        mock_hvac = MagicMock()
        mock_hvac.Client.side_effect = Exception("vault unavailable")

        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            val = _get_from_vault("ANY_KEY")

        assert val is None


class TestGetSecret:
    def test_falls_through_to_aws(self, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_AWS_SECRET_ID", "arn:aws:sm:us-east-1:123:secret:my-secret")
        monkeypatch.delenv("AWS_FETCHED_SECRET", raising=False)

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"AWS_SECRET_KEY": "aws-val"})
        }
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            val = get_secret("AWS_SECRET_KEY")

        assert val == "aws-val"

    def test_falls_through_to_vault(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.delenv("ALERTTRIAGE_AWS_SECRET_ID", raising=False)

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"VAULT_ONLY_KEY": "from-vault"}}
        }
        mock_hvac = MagicMock()
        mock_hvac.Client.return_value = mock_client

        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            val = get_secret("VAULT_ONLY_KEY")

        assert val == "from-vault"

    def test_returns_default_when_nothing_found(self, monkeypatch):
        monkeypatch.delenv("ALERTTRIAGE_AWS_SECRET_ID", raising=False)
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        val = get_secret("TRULY_MISSING", default="fallback")
        assert val == "fallback"


class TestPrefetch:
    def test_prefetch_no_aws_secret_id(self, monkeypatch):
        monkeypatch.delenv("ALERTTRIAGE_AWS_SECRET_ID", raising=False)
        prefetch(["KEY_A", "KEY_B"])  # Should be a no-op

    def test_prefetch_populates_cache(self, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_AWS_SECRET_ID", "my-secret")

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"PREFETCH_A": "val-a", "PREFETCH_B": "val-b"})
        }
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            prefetch(["PREFETCH_A", "PREFETCH_B"])
            val_a = get_secret("PREFETCH_A")
            val_b = get_secret("PREFETCH_B")

        assert val_a == "val-a"
        assert val_b == "val-b"

    def test_prefetch_exception_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_AWS_SECRET_ID", "my-secret")
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("connection error")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            prefetch(["KEY"])  # Should not raise
