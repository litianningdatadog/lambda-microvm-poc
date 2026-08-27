import unittest

import httpx

from app import BASE_PATH, app


class AppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    async def test_lifecycle_hooks_return_empty_200(self):
        for hook in ("validate", "ready", "run", "resume", "suspend", "terminate"):
            with self.subTest(hook=hook):
                response = await self.client.post(f"{BASE_PATH}/{hook}", json={"microVmId": "vm-local"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"")

    async def test_execute_runs_python_snippet(self):
        response = await self.client.post("/execute", json={"code": "print(1 + 1)"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "output": "2\n", "stderr": ""})


if __name__ == "__main__":
    unittest.main()
