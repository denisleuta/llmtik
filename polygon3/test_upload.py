"""Проверки повторов соединения без настоящей отправки решения."""
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from polygon3 import client

try:
    import requests
except ImportError:
    requests = None


@unittest.skipIf(requests is None, 'пул HTTPS использует необязательный requests')
class UploadTest(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        session_patch = patch.object(requests, 'Session')
        factory = session_patch.start()
        factory.return_value.__enter__.return_value = self.session
        self.addCleanup(session_patch.stop)
        pause_patch = patch.object(client.time, 'sleep')
        pause_patch.start()
        self.addCleanup(pause_patch.stop)

    def put(self):
        return client.upload(Path(__file__).with_name('negotiation_agent.py'),
                             base_url=client.COURSE_URL, token='fake_token')

    def response(self, status, *chunks):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.iter_content.return_value = iter(chunks)
        return response

    def test_get_retries_but_post_happens_once(self):
        self.session.get.side_effect = [requests.exceptions.ReadTimeout(), self.response(200)]
        self.session.post.return_value = self.response(200, b'{"job_id": ', b'219}')
        with patch.dict(os.environ, {'SSL_CERT_FILE': '/tmp/test-ca.pem'}):
            self.assertEqual(self.put()['job_id'], 219)
        self.assertEqual(self.session.get.call_count, 2)
        self.session.post.assert_called_once()
        self.assertNotIn('headers', self.session.get.call_args.kwargs)
        for method in (self.session.get, self.session.post):
            self.assertEqual(method.call_args.kwargs['verify'], '/tmp/test-ca.pem')
            self.assertFalse(method.call_args.kwargs['allow_redirects'])

    def test_cannot_connect_never_posts(self):
        self.session.get.side_effect = requests.exceptions.ReadTimeout()
        with self.assertRaisesRegex(ValueError, 'файл не отправлен'):
            self.put()
        self.session.post.assert_not_called()

    def test_post_timeout_is_not_retried(self):
        self.session.get.return_value = self.response(200)
        self.session.post.side_effect = requests.exceptions.ReadTimeout()
        with self.assertRaisesRegex(ValueError, 'историю попыток'):
            self.put()
        self.session.post.assert_called_once()

    def test_submission_rejection_surfaces_server_reason(self):
        self.session.get.return_value = self.response(200)
        self.session.post.return_value = self.response(429, b'{"detail":"quota"}')
        with self.assertRaisesRegex(ValueError, '429: quota'):
            self.put()
        self.session.post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
