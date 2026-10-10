import json
import os
import unittest
from unittest.mock import patch
import httpx
from haiku import HaikuError, generate


class HaikuTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "HAIKU_MODEL": "claude-haiku-5-5"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [{"role":"system", "content":"Orientações seguras"}, {"role":"user", "content":"Como digitar Ç?"}]

    def test_native_contract_and_effort_levels(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(level=level):
                def handler(request):
                    body = json.loads(request.content)
                    self.assertEqual(request.headers["x-api-key"], "test-key")
                    self.assertEqual(body["model"], "claude-haiku-5-5")
                    self.assertEqual(body["system"], "Orientações seguras")
                    self.assertEqual(body["messages"], [self.messages[1]])
                    self.assertEqual(body["output_config"]["effort"], level)
                    self.assertEqual(body["thinking"]["type"], "adaptive" if level in ("xhigh","max") else "disabled")
                    self.assertNotIn("temperature", body)
                    return httpx.Response(200, json={"stop_reason":"end_turn", "content":[{"type":"thinking","thinking":"interno"}, {"type":"text","text":"Resposta pública"}], "usage":{}})
                self.assertEqual(generate(self.messages, effort=level, transport=httpx.MockTransport(handler)), "Resposta pública")

    def test_errors_do_not_reveal_body_or_credentials(self):
        with self.assertRaisesRegex(HaikuError, "HTTP 401") as error:
            generate(self.messages, transport=httpx.MockTransport(lambda req: httpx.Response(401,text="test-key")))
        self.assertNotIn("test-key",str(error.exception))

    def test_truncated_response_rejected(self):
        with self.assertRaises(HaikuError):
            generate(self.messages,transport=httpx.MockTransport(lambda req: httpx.Response(200,json={"stop_reason":"max_tokens","content":[{"type":"text","text":"JSON parcial"}]})))

    def test_invalid_effort_prevents_network(self):
        with self.assertRaisesRegex(HaikuError,"Effort inválido"):
            generate(self.messages,effort="adaptive",transport=httpx.MockTransport(lambda req: self.fail("network")))

    def test_timeout_sanitized(self):
        def handler(request):
            raise httpx.ReadTimeout("test-key")
        with self.assertRaisesRegex(HaikuError,"indisponível"):
            generate(self.messages,transport=httpx.MockTransport(handler))
