import json
import unittest

from django.test import Client

from app import BASE_PATH


class AppTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_health(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'status': 'healthy'})

    def test_lifecycle_hooks_return_empty_200(self):
        for hook in ('validate', 'ready', 'run', 'resume', 'suspend', 'terminate'):
            with self.subTest(hook=hook):
                response = self.client.post(
                    f'{BASE_PATH}/{hook}',
                    data=json.dumps({'microVmId': 'vm-local'}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'')

    def test_execute_runs_python_snippet(self):
        response = self.client.post(
            '/execute',
            data=json.dumps({'code': 'print(1 + 1)'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'success': True, 'output': '2\n', 'stderr': ''})


if __name__ == '__main__':
    unittest.main()
