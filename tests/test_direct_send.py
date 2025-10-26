import unittest
from unittest import mock

from app import db
from app.main import DirectSendRequest, direct_send


class DirectSendEndpointTest(unittest.TestCase):
    def test_direct_send_sends_email_when_not_suppressed(self) -> None:
        request = DirectSendRequest(
            to_email="test@example.com",
            body_html="<p>Hi there</p>",
            dry_run=False,
            tone=None,
        )

        with mock.patch("app.main.send_email_with_fallback", return_value=True) as mock_send:
            response = direct_send(request, None)

        self.assertTrue(response.sent)
        self.assertIsNotNone(response.id)
        self.assertIsNone(response.reason)
        mock_send.assert_called_once()

    def test_direct_send_respects_suppression_list(self) -> None:
        with db.get_session() as session:
            db.add_to_suppression(session, "suppressed@example.com")

        request = DirectSendRequest(
            to_email="suppressed@example.com",
            body_html="<p>Hi there</p>",
            dry_run=False,
            tone=None,
        )

        with mock.patch("app.main.send_email_with_fallback", return_value=True) as mock_send:
            response = direct_send(request, None)

        self.assertFalse(response.sent)
        self.assertEqual(response.reason, "suppressed")
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
