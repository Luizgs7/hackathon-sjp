import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app
import jev


def decision(choices):
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 120, "output_tokens": 20},
            "criteria_version": "dataforge-1",
            "answers": {k: {"type": "choice", "choice": v, "confidence": 0.9}
                        for k, v in choices.items()}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key", "JEV_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.questions = {"acao": jev.choice("Próximo passo?", {"orientar": "Dúvida simples", "helpdesk": "Técnico"})}

    def payload(self):
        result = decision({"acao": "orientar"})
        result["answers"]["acao"]["probabilities"] = {"orientar": 0.95, "helpdesk": 0.05}
        return result

    def test_request_and_usage(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["state"], "Como digitar Ç?")
            self.assertEqual(body["questions"], self.questions)
            return httpx.Response(200, json=self.payload())
        result = jev.evaluate("Como digitar Ç?", self.questions, transport=httpx.MockTransport(handler))
        self.assertEqual(result["usage"]["input_tokens"], 120)

    def test_http_errors_are_sanitized_and_not_retried(self):
        for status in (401, 422, 429, 529):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, text="secret diagnostic")
                with self.assertRaises(jev.JevError) as error:
                    jev.evaluate("relato", self.questions, transport=httpx.MockTransport(handler))
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("test-key")
        with self.assertRaisesRegex(jev.JevError, "indisponível"):
            jev.evaluate("relato", self.questions, transport=httpx.MockTransport(handler))

    def test_invalid_response(self):
        cases = [{}, self.payload(), self.payload(), self.payload()]
        cases[1]["answers"]["acao"]["choice"] = "fora-do-catalogo"
        cases[2]["answers"]["acao"]["confidence"] = True
        cases[3]["answers"]["acao"]["probabilities"]["orientar"] = float("nan")
        for payload in cases:
            with self.subTest(payload=str(payload)):
                # JSON inválido/NaN também pode ocorrer como bytes numa resposta externa.
                transport = httpx.MockTransport(lambda req: httpx.Response(200, content=json.dumps(payload)))
                with self.assertRaises(jev.JevError):
                    jev.evaluate("relato", self.questions, transport=transport)

    def test_disabled_does_not_call_network(self):
        with patch.dict(os.environ, {"JEV_ENABLED": "false"}):
            with self.assertRaises(jev.JevError):
                jev.evaluate("relato", self.questions,
                             transport=httpx.MockTransport(lambda req: self.fail("network called")))

    def test_minimal_context(self):
        task = {"titulo": "Impressora", "descricao": "Não imprime", "secretaria": "Educação",
                "solicitante": "Pessoa", "contato": "telefone", "local": "Sala 1"}
        with patch("jev.evaluate", return_value={}) as call:
            jev.classify_task(task, [{"id": 2, "nome": "Impressora", "setor": "Suporte"}])
        self.assertEqual(set(call.call_args.args[0]), {"titulo", "descricao", "secretaria"})


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dbpatch = patch.object(app, "DB_PATH", Path(self.temp.name) / "test.db")
        self.seedpatch = patch.object(app, "SEED_HISTORICO", False)
        self.dbpatch.start()
        self.seedpatch.start()
        self.addCleanup(self.dbpatch.stop)
        self.addCleanup(self.seedpatch.stop)
        app.init_db()
        with app.db() as c:
            self.tid = c.execute("SELECT id FROM tarefas LIMIT 1").fetchone()["id"]
            self.tipo = c.execute("SELECT id FROM tipos WHERE setor_id=2 LIMIT 1").fetchone()["id"]
            c.execute("UPDATE tarefas SET ia_status='analisando' WHERE id=?", (self.tid,))
            c.execute("INSERT INTO autoatendimento(token,criado_em) VALUES('test',?)", (app.agora(),))
            c.execute("INSERT INTO auto_msgs(token,autor,texto,criado_em) VALUES('test','servidor','Como digitar Ç?',?)", (app.agora(),))

    def task(self):
        with app.db() as c:
            return dict(c.execute("SELECT * FROM tarefas WHERE id=?", (self.tid,)).fetchone())

    def test_triage_is_only_a_suggestion(self):
        before = self.task()
        with patch.object(app, "classify_task", return_value=decision({"tipo": str(self.tipo), "prioridade": "P1"})):
            app.triar(self.tid)
        after = self.task()
        self.assertEqual(after["ia_fonte"], "jev")
        self.assertEqual(json.loads(after["ia_json"])["prioridade"], "P1")
        for field in ("prioridade", "executor_id", "status"):
            self.assertEqual(before[field], after[field])

    def test_api_failure_keeps_rules(self):
        with patch.object(app, "classify_task", side_effect=jev.JevError("JEV HTTP 401")):
            app.triar(self.tid)
        self.assertEqual(self.task()["ia_fonte"], "regras")

    def test_abstention_requires_review(self):
        with patch.object(app, "classify_task", return_value=decision({"tipo": "revisar", "prioridade": "revisar"})):
            app.triar(self.tid)
        result = json.loads(self.task()["ia_json"])
        self.assertTrue(result["revisao_necessaria"])
        self.assertFalse(result["resolvivel_no_atendimento"])

    def test_late_result_cannot_overwrite_human_review(self):
        def classifier(*args):
            with app.db() as c:
                c.execute("UPDATE tarefas SET ia_status='rejeitada' WHERE id=?", (self.tid,))
            return decision({"tipo": str(self.tipo), "prioridade": "P1"})
        with patch.object(app, "classify_task", side_effect=classifier):
            app.triar(self.tid)
        self.assertEqual(self.task()["ia_status"], "rejeitada")

    def test_late_result_cannot_overwrite_newer_request(self):
        def classifier(*args):
            with app.db() as c:
                c.execute("UPDATE tarefas SET ia_json=? WHERE id=?", ('{"request_id":"newer"}', self.tid))
            return decision({"tipo": str(self.tipo), "prioridade": "P1"})
        with patch.object(app, "classify_task", side_effect=classifier):
            app.triar(self.tid)
        self.assertEqual(json.loads(self.task()["ia_json"])["request_id"], "newer")

    def test_route_to_helpdesk_without_creating_os(self):
        with app.db() as c:
            count = c.execute("SELECT count(*) FROM tarefas").fetchone()[0]
            with patch.object(app, "route_conversation", return_value=decision({"acao": "helpdesk"})), patch.object(app, "llm") as llm:
                text, source = app.responder_autoatendimento(c, "test", "Como digitar Ç?")
            self.assertIn("Abrir solicitação", text)
            self.assertEqual(source, "jev")
            llm.assert_not_called()
            self.assertEqual(c.execute("SELECT count(*) FROM tarefas").fetchone()[0], count)

    def test_simple_guidance_has_no_duplicate_prompt(self):
        with app.db() as c:
            with patch.object(app, "route_conversation", return_value=decision({"acao": "orientar"})), patch.object(app, "llm", return_value="Orientação") as llm:
                app.responder_autoatendimento(c, "test", "Como digitar Ç?")
        user_messages = [m for m in llm.call_args.args[0] if m["role"] == "user"]
        self.assertEqual(len(user_messages), 1)

    def test_clarification_and_risk_do_not_generate_instructions(self):
        for action, expected in (("esclarecer", "mensagem de erro"), ("risco", "Afaste-se")):
            with self.subTest(action=action), app.db() as c:
                with patch.object(app, "route_conversation", return_value=decision({"acao": action})), patch.object(app, "llm") as llm:
                    text, source = app.responder_autoatendimento(c, "test", "Preciso de ajuda")
                self.assertIn(expected, text)
                self.assertEqual(source, "jev")
                llm.assert_not_called()

    def test_api_failure_does_not_block_chat(self):
        with app.db() as c:
            with patch.object(app, "route_conversation", side_effect=jev.JevError("timeout")), patch.object(app, "llm", return_value="Pode explicar o problema?"):
                text, source = app.responder_autoatendimento(c, "test", "Preciso de ajuda")
            self.assertEqual(source, "llm")
            self.assertTrue(text)
            self.assertIsNone(c.execute("SELECT jev_json FROM autoatendimento WHERE token='test'").fetchone()[0])

    def test_chat_http_endpoint_and_template(self):
        client = TestClient(app.app)
        with patch.object(app, "route_conversation", return_value=decision({"acao": "helpdesk"})), patch.object(app, "llm") as llm:
            response = client.post("/servidor/chat", data={"pergunta": "Impressora quebrada"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Helpdesk", response.text)
        self.assertIn("auto_token", response.cookies)
        llm.assert_not_called()

    def test_database_migration_is_repeatable(self):
        app.init_db()
        with app.db() as c:
            columns = [r["name"] for r in c.execute("PRAGMA table_info(autoatendimento)")]
        self.assertEqual(columns.count("jev_json"), 1)

    def test_queue_reorders_without_changing_priority(self):
        with app.db() as c:
            c.execute("UPDATE tarefas SET status='novo',prioridade='P3',ia_json=NULL")
            c.execute("UPDATE tarefas SET prioridade=NULL,ia_json=? WHERE id=?", ('{"prioridade":"P1"}', self.tid))
            self.assertEqual(app.fila_atendimento(c)[0]["t"]["id"], self.tid)
            self.assertIsNone(c.execute("SELECT prioridade FROM tarefas WHERE id=?", (self.tid,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
