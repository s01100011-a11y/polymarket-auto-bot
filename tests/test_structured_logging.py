import logging
import os
import unittest
from unittest.mock import patch

from app import structured_logging


class StructuredLoggingTests(unittest.TestCase):
    def test_configured_level_uses_log_level_env(self):
        for value, expected in (
            ("DEBUG", logging.DEBUG),
            ("warning", logging.WARNING),
            ("ERROR", logging.ERROR),
        ):
            with self.subTest(value=value), patch.dict(os.environ, {"LOG_LEVEL": value}):
                self.assertEqual(structured_logging._configured_level(), expected)

    def test_invalid_log_level_falls_back_to_info(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "not-a-level"}):
            self.assertEqual(structured_logging._configured_level(), logging.INFO)


if __name__ == "__main__":
    unittest.main()
