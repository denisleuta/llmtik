"""Настройки из временных файлов; настоящий .env и сеть не используются."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import polygon
import solution


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settings(self, module, folder, values=None):
        with patch.dict(os.environ, values or {}, clear=True):
            with patch.object(module, '__file__', str(folder / 'client.py')):
                return module._transport_settings()

    def test_reads_only_local_file(self):
        (self.root / '.env').write_text(
            'POLYGON_TOKEN=test-local-token\nPOLYGON_URL=https://local.invalid\n', encoding='utf-8')
        for module in (polygon, solution):
            with self.subTest(module=module.__name__):
                url, token, model = self.settings(module, self.root)
                self.assertEqual((url, token, model),
                                 ('https://local.invalid', 'test-local-token', 'deepseek-flash'))

    def test_process_environment_has_priority(self):
        (self.root / '.env').write_text(
            'POLYGON_TOKEN=file-token\nPOLYGON_URL=https://file.invalid\n', encoding='utf-8')
        for module in (polygon, solution):
            with self.subTest(module=module.__name__):
                url, token, _ = self.settings(module, self.root, {
                    'POLYGON_TOKEN': 'process-token', 'POLYGON_URL': 'https://process.invalid'})
                self.assertEqual((url, token), ('https://process.invalid', 'process-token'))

    def test_does_not_search_parent_directory(self):
        (self.root / '.env').write_text('POLYGON_TOKEN=parent-token\n', encoding='utf-8')
        child = self.root / 'child'
        child.mkdir()
        for module in (polygon, solution):
            with self.subTest(module=module.__name__):
                self.assertEqual(self.settings(module, child)[1], '')

    def test_missing_file_does_not_require_dotenv(self):
        for module in (polygon, solution):
            with self.subTest(module=module.__name__):
                with patch.dict(sys.modules, {'dotenv': None}):
                    self.assertEqual(self.settings(module, self.root, {
                        'POLYGON_TOKEN': 'process-token'})[1], 'process-token')

    def test_server_credentials_still_work(self):
        for module in (polygon, solution):
            with self.subTest(module=module.__name__):
                url, token, _ = self.settings(module, self.root, {
                    'LLM_BASE_URL': 'http://server.invalid', 'LLM_API_KEY': 'job-token'})
                self.assertEqual((url, token), ('http://server.invalid', 'job-token'))

    def test_submit_loads_local_token_without_network(self):
        (self.root / '.env').write_text(
            'POLYGON_TOKEN=test-local-token\nPOLYGON_URL=https://local.invalid\n', encoding='utf-8')
        submitted = self.root / 'solution.py'
        submitted.write_text('def act(observation, memory): return {}\n', encoding='utf-8')
        response = Mock(status_code=200)
        response.json.return_value = {'job_id': 1}
        status = Mock(status_code=200)
        status.json.return_value = {'rules_versions': {'c4': 3}}
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(polygon, '__file__', str(self.root / 'polygon.py')), \
                patch('requests.get', return_value=status), \
                patch('requests.post', return_value=response) as post, redirect_stdout(output):
            polygon.submit(str(submitted))
        self.assertEqual(post.call_args.args[0], 'https://local.invalid/submit')
        self.assertEqual(post.call_args.kwargs['headers'],
                         {'Authorization': 'Bearer test-local-token'})
        self.assertNotIn('test-local-token', output.getvalue())


if __name__ == '__main__':
    unittest.main()
