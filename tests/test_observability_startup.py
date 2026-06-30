"""Unit tests for the observability platform startup wiring (task 19.1).

Validates requirements:
- R9.1: All Trace_Store writes happen outside the request-response path.
- R17.1: Same for log writes.

These tests verify that calling ``_start_observability_platform()`` correctly
wires the background flush workers, log handler, and retention scheduler.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_startup_flag():
    """Reset the module-level idempotency flag before each test."""
    import rag_system.api as api_mod

    original = api_mod._observability_started
    api_mod._observability_started = False
    yield
    api_mod._observability_started = original


@pytest.fixture
def mock_settings():
    """Create a mock settings object with tracing enabled."""
    settings = MagicMock()
    settings.tracing_enabled = True
    settings.trace_sample_rate = 1.0
    settings.trace_retention_hours = 72
    settings.log_retention_hours = 48
    settings.retention_interval_hours = 12
    settings.trace_buffer_capacity = 10_000
    settings.log_buffer_capacity = 10_000
    return settings


class TestStartObservabilityPlatform:
    """Tests for _start_observability_platform."""

    @patch("rag_system.api.get_settings")
    @patch("rag_system.api.get_trace_store")
    @patch("rag_system.api.get_log_store")
    @patch("rag_system.observability_tracing.get_span_recorder")
    def test_attaches_log_handler_to_root_logger(
        self,
        mock_get_recorder,
        mock_get_log_store,
        mock_get_trace_store,
        mock_get_settings,
        mock_settings,
    ):
        """The startup function adds a TracePersistingLogHandler to the root logger."""
        from rag_system.observability_tracing.buffers import BoundedSpanBuffer
        from rag_system.observability_tracing.log_handler import (
            TracePersistingLogHandler,
        )

        mock_get_settings.return_value = mock_settings
        mock_recorder = MagicMock()
        mock_recorder._span_buffer = BoundedSpanBuffer()
        mock_get_recorder.return_value = mock_recorder

        root_logger = logging.getLogger()
        initial_handler_count = len(root_logger.handlers)

        import rag_system.api as api_mod

        api_mod._start_observability_platform()

        # A TracePersistingLogHandler should have been added
        new_handlers = root_logger.handlers[initial_handler_count:]
        assert any(isinstance(h, TracePersistingLogHandler) for h in new_handlers)

        # Clean up the handler to avoid polluting other tests
        for h in new_handlers:
            if isinstance(h, TracePersistingLogHandler):
                root_logger.removeHandler(h)

    @patch("rag_system.api.get_settings")
    @patch("rag_system.api.get_trace_store")
    @patch("rag_system.api.get_log_store")
    @patch("rag_system.observability_tracing.get_span_recorder")
    def test_starts_trace_flush_worker(
        self,
        mock_get_recorder,
        mock_get_log_store,
        mock_get_trace_store,
        mock_get_settings,
        mock_settings,
    ):
        """The startup function starts the TraceFlushWorker daemon thread."""
        from rag_system.observability_tracing.buffers import BoundedSpanBuffer

        mock_get_settings.return_value = mock_settings
        mock_recorder = MagicMock()
        mock_recorder._span_buffer = BoundedSpanBuffer()
        mock_get_recorder.return_value = mock_recorder

        import rag_system.api as api_mod

        with patch(
            "rag_system.api.TraceFlushWorker"
        ) as MockTraceFlush:
            mock_worker = MagicMock()
            MockTraceFlush.return_value = mock_worker

            api_mod._start_observability_platform()

            MockTraceFlush.assert_called_once()
            mock_worker.start.assert_called_once()

        # Clean up log handler
        root_logger = logging.getLogger()
        from rag_system.observability_tracing.log_handler import (
            TracePersistingLogHandler,
        )
        for h in list(root_logger.handlers):
            if isinstance(h, TracePersistingLogHandler):
                root_logger.removeHandler(h)

    @patch("rag_system.api.get_settings")
    @patch("rag_system.api.get_trace_store")
    @patch("rag_system.api.get_log_store")
    @patch("rag_system.observability_tracing.get_span_recorder")
    def test_starts_log_flush_worker(
        self,
        mock_get_recorder,
        mock_get_log_store,
        mock_get_trace_store,
        mock_get_settings,
        mock_settings,
    ):
        """The startup function starts the LogFlushWorker daemon thread."""
        from rag_system.observability_tracing.buffers import BoundedSpanBuffer

        mock_get_settings.return_value = mock_settings
        mock_recorder = MagicMock()
        mock_recorder._span_buffer = BoundedSpanBuffer()
        mock_get_recorder.return_value = mock_recorder

        import rag_system.api as api_mod

        with patch(
            "rag_system.api.LogFlushWorker"
        ) as MockLogFlush:
            mock_worker = MagicMock()
            MockLogFlush.return_value = mock_worker

            api_mod._start_observability_platform()

            MockLogFlush.assert_called_once()
            mock_worker.start.assert_called_once()

        # Clean up log handler
        root_logger = logging.getLogger()
        from rag_system.observability_tracing.log_handler import (
            TracePersistingLogHandler,
        )
        for h in list(root_logger.handlers):
            if isinstance(h, TracePersistingLogHandler):
                root_logger.removeHandler(h)

    @patch("rag_system.api.get_settings")
    @patch("rag_system.api.get_trace_store")
    @patch("rag_system.api.get_log_store")
    @patch("rag_system.observability_tracing.get_span_recorder")
    def test_starts_retention_scheduler(
        self,
        mock_get_recorder,
        mock_get_log_store,
        mock_get_trace_store,
        mock_get_settings,
        mock_settings,
    ):
        """The startup function starts the RetentionScheduler daemon thread."""
        from rag_system.observability_tracing.buffers import BoundedSpanBuffer

        mock_get_settings.return_value = mock_settings
        mock_recorder = MagicMock()
        mock_recorder._span_buffer = BoundedSpanBuffer()
        mock_get_recorder.return_value = mock_recorder

        import rag_system.api as api_mod

        with patch(
            "rag_system.api.RetentionScheduler"
        ) as MockRetention:
            mock_scheduler = MagicMock()
            MockRetention.return_value = mock_scheduler

            api_mod._start_observability_platform()

            MockRetention.assert_called_once_with(
                trace_store=mock_get_trace_store.return_value,
                log_store=mock_get_log_store.return_value,
                trace_retention_hours=mock_settings.trace_retention_hours,
                log_retention_hours=mock_settings.log_retention_hours,
                interval_hours=mock_settings.retention_interval_hours,
            )
            mock_scheduler.start.assert_called_once()

        # Clean up log handler
        root_logger = logging.getLogger()
        from rag_system.observability_tracing.log_handler import (
            TracePersistingLogHandler,
        )
        for h in list(root_logger.handlers):
            if isinstance(h, TracePersistingLogHandler):
                root_logger.removeHandler(h)

    @patch("rag_system.api.get_settings")
    @patch("rag_system.api.get_trace_store")
    @patch("rag_system.api.get_log_store")
    @patch("rag_system.observability_tracing.get_span_recorder")
    def test_idempotent_when_called_twice(
        self,
        mock_get_recorder,
        mock_get_log_store,
        mock_get_trace_store,
        mock_get_settings,
        mock_settings,
    ):
        """Calling _start_observability_platform twice only wires once."""
        from rag_system.observability_tracing.buffers import BoundedSpanBuffer

        mock_get_settings.return_value = mock_settings
        mock_recorder = MagicMock()
        mock_recorder._span_buffer = BoundedSpanBuffer()
        mock_get_recorder.return_value = mock_recorder

        import rag_system.api as api_mod

        root_logger = logging.getLogger()
        initial_handler_count = len(root_logger.handlers)

        api_mod._start_observability_platform()
        handler_count_after_first = len(root_logger.handlers)
        # First startup attaches the log-capture handler.
        assert handler_count_after_first > initial_handler_count

        # Second call should be a no-op
        api_mod._start_observability_platform()
        handler_count_after_second = len(root_logger.handlers)

        assert handler_count_after_second == handler_count_after_first

        # Clean up
        from rag_system.observability_tracing.log_handler import (
            TracePersistingLogHandler,
        )
        for h in list(root_logger.handlers):
            if isinstance(h, TracePersistingLogHandler):
                root_logger.removeHandler(h)

    @patch("rag_system.api.get_settings")
    def test_skipped_when_tracing_disabled(self, mock_get_settings):
        """The lifespan does not wire observability when tracing is disabled."""
        mock_settings = MagicMock()
        mock_settings.tracing_enabled = False
        mock_settings.auth_enabled = False
        mock_get_settings.return_value = mock_settings

        import rag_system.api as api_mod

        with patch.object(api_mod, "_start_observability_platform") as mock_start:
            import asyncio

            async def _run() -> None:
                async with api_mod.lifespan(api_mod.app):
                    pass

            asyncio.run(_run())
            mock_start.assert_not_called()
