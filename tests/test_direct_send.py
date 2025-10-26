import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


TEST_DB_PATH = Path("tmp/test_direct_send.db")


if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["API_BEARER_TOKEN"] = "testtoken"

from app import db  # noqa: E402
from app.main import DirectSendRequest, direct_send_endpoint  # noqa: E402


class DirectSendEndpointTests(TestCase):
    def setUp(self) -> None:
        db.Base.metadata.drop_all(bind=db.engine)
        db.Base.metadata.create_all(bind=db.engine)

    def test_direct_send_dispatches_when_not_suppressed(self) -> None:
        request = DirectSendRequest(
            to_email="recipient@example.com",
            body_html="<p>Custom body</p>",
            dry_run=False,
        )

        with patch("app.main.send_email_with_fallback", return_value=True) as mock_send:
            response = direct_send_endpoint(request, None)

        self.assertTrue(response.sent)
        self.assertIsNotNone(response.id)
        self.assertIsNone(response.reason)
        mock_send.assert_called_once()
