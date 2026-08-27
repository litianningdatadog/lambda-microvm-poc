import unittest

from app import BASE_PATH, app


class AppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = app.test_client()

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await response.get_json(), {"status": "healthy"})

    async def test_lifecycle_hooks_return_empty_200(self):
        for hook in ("validate", "ready", "run", "resume", "suspend", "terminate"):
            with self.subTest(hook=hook):
                response = await self.client.post(f"{BASE_PATH}/{hook}", json={"microVmId": "vm-local"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(await response.get_data(), b"")

    async def test_execute_runs_python_snippet(self):
        response = await self.client.post("/execute", json={"code": "print(1 + 1)"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await response.get_json(), {"success": True, "output": "2\n", "stderr": ""})


if __name__ == "__main__":
    unittest.main()
