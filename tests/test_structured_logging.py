import io
import json
import logging
import os
import unittest
from unittest.mock import patch

from app.structured_logging import JsonFormatter, configure_logging, configured_log_level, log_event


class StructuredLoggingTests(unittest.TestCase):
    def tearDown(self):
        logging.getLogger().handlers.clear()

    def test_log_level_reads_environment(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            self.assertEqual(configured_log_level(), logging.DEBUG)
        with patch.dict(os.environ, {"LOG_LEVEL": "warning"}):
            self.assertEqual(configured_log_level(), logging.WARNING)
        with patch.dict(os.environ, {"LOG_LEVEL": "not-a-level"}):
            self.assertEqual(configured_log_level(), logging.INFO)

    def test_configure_logging_uses_json_formatter_and_level(self):
        root = logging.getLogger()
        root.handlers.clear()
        with patch.dict(os.environ, {"LOG_LEVEL": "ERROR"}):
            configure_logging()
        self.assertEqual(root.level, logging.ERROR)
        self.assertEqual(len(root.handlers), 1)
        self.assertIsInstance(root.handlers[0].formatter, JsonFormatter)

    def test_log_event_serializes_structured_context(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.structured")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)

        log_event(
            logger,
            "trade_submitted",
            trade_id="abc123",
            signal_id="sig-1",
            status="ORDER_SUBMITTED",
            budget_usdc="5",
        )
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["event"], "trade_submitted")
        self.assertEqual(payload["trade_id"], "abc123")
        self.assertEqual(payload["signal_id"], "sig-1")
        self.assertEqual(payload["status"], "ORDER_SUBMITTED")
        self.assertEqual(payload["budget_usdc"], "5")
        self.assertIn("ts", payload)


if __name__ == "__main__":
    unittest.main()
