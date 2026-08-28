from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import litellm
except ModuleNotFoundError as exc:
    if exc.name != "litellm":
        raise
    litellm = None
    no_log_callback = None
else:
    from tools.litellm_no_log import no_log_callback


@unittest.skipIf(litellm is None, "the pinned LiteLLM runtime is not installed")
class LiteLLMNoLogRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert litellm is not None
        self._callbacks = litellm.callbacks
        litellm.callbacks = [no_log_callback]
        self.records: list[tuple[str, dict[str, object]]] = []
        self._records_lock = threading.Lock()
        records = self.records
        records_lock = self._records_lock

        class CaptureHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                with records_lock:
                    records.append((self.path, body))
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"captured","type":"invalid_request_error"}}'
                )

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.router = litellm.Router(
            model_list=[
                {
                    "model_name": "openai-test",
                    "litellm_params": {
                        "model": "azure/gpt-5.6-sol",
                        "api_base": base
                        + "/v1/responses?_litellm_route=/openai/responses",
                        "api_key": "dummy-openai-key",
                        "api_version": "v1",
                        "extra_body": {"no-log": True},
                        "max_retries": 0,
                    },
                    "model_info": {"id": "openai-test", "base_model": "gpt-5.6-sol"},
                },
                {
                    "model_name": "anthropic-test",
                    "litellm_params": {
                        "model": "anthropic/claude-sonnet-4-6",
                        "api_base": base,
                        "api_key": "dummy-anthropic-key",
                        "extra_body": {"no-log": True},
                        "max_retries": 0,
                    },
                    "model_info": {"id": "anthropic-test"},
                },
                {
                    "model_name": "gemini-test",
                    "litellm_params": {
                        "model": "gemini/gemini-3.6-flash",
                        "api_base": base + "/v1beta",
                        "api_key": "dummy-gemini-key",
                        "extra_body": {"no-log": True},
                        "max_retries": 0,
                    },
                    "model_info": {"id": "gemini-test"},
                },
            ],
            num_retries=0,
        )

    async def asyncTearDown(self) -> None:
        assert litellm is not None
        litellm.callbacks = self._callbacks
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    async def _assert_native_calls(self, stream: bool) -> None:
        gemini_method = (
            self.router.agenerate_content_stream
            if stream
            else self.router.agenerate_content
        )
        calls = [
            self.router.aresponses(
                model="openai-test",
                input="capture",
                stream=stream,
                extra_body={"no-log": False},
            ),
            self.router.aanthropic_messages(
                model="anthropic-test",
                messages=[{"role": "user", "content": "capture"}],
                max_tokens=1,
                stream=stream,
                extra_body={"no-log": False},
            ),
            gemini_method(
                model="gemini-test",
                contents=[{"role": "user", "parts": [{"text": "capture"}]}],
                extra_body={"no-log": False},
            ),
        ]
        results = await asyncio.gather(*calls, return_exceptions=True)
        self.assertTrue(all(isinstance(result, Exception) for result in results))

        self.assertEqual(3, len(self.records))
        paths = [path for path, _body in self.records]
        self.assertTrue(any(path.startswith("/v1/responses?") for path in paths))
        self.assertIn("/v1/messages", paths)
        gemini_suffix = (
            ":streamGenerateContent" if stream else ":generateContent"
        )
        self.assertTrue(any(gemini_suffix in path for path in paths))
        for _path, body in self.records:
            self.assertIs(body.get("no-log"), True)
            self.assertNotIn("extra_body", body)

    async def test_all_native_upstreams_force_top_level_no_log_true(self) -> None:
        await self._assert_native_calls(stream=False)

    async def test_all_native_streaming_upstreams_force_top_level_no_log_true(self) -> None:
        await self._assert_native_calls(stream=True)


if __name__ == "__main__":
    unittest.main()
