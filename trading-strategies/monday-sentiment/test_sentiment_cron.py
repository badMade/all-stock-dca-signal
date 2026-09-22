import sys
import unittest
from unittest.mock import MagicMock, patch

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = MagicMock()

import sentiment_cron  # noqa: E402


class TestSentimentCron(unittest.TestCase):
    @patch("sentiment_cron.log")
    def test_send_email_strips_credentials(self, mock_logger):
        with patch("sentiment_cron.smtplib.SMTP") as mock_smtp:
            import smtplib

            mock_server = mock_smtp.return_value.__enter__.return_value
            mock_server.login.side_effect = smtplib.SMTPException("failed with password super-secret-password-123")

            sentiment_cron.send_email("body", "subject", "user", "super-secret-password-123", "recip")

            for call in mock_logger.error.call_args_list:
                assert "super-secret-password-123" not in str(call)
                assert "***" in str(call)
