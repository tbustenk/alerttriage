"""Unit tests for the logger module."""

from __future__ import annotations

from alerttriage.src.logger import configure_logging, get_logger, bind_client_context, clear_context


class TestConfigureLogging:
    def test_console_mode(self):
        configure_logging(level="WARNING", json_output=False)

    def test_json_mode(self):
        configure_logging(level="DEBUG", json_output=True)

    def test_default_params(self):
        configure_logging()


class TestGetLogger:
    def test_returns_logger(self):
        log = get_logger("test.module")
        assert log is not None

    def test_different_names_return_loggers(self):
        log1 = get_logger("module.a")
        log2 = get_logger("module.b")
        assert log1 is not None
        assert log2 is not None


class TestContextBinding:
    def test_bind_and_clear(self):
        bind_client_context("test-client")
        clear_context()
