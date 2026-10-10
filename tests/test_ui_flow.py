"""Fluxo completo na interface DataForge: páginas, fragmentos htmx, permissões e caminhos de atendimento.

Usa banco SQLite temporário, dados fictícios e respostas simuladas de IA (nenhuma chamada externa)."""
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from fastapi.testclient import TestClient

import app
from jev import JevError


class Inventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.labels_for, self.controls, self.hrefs, self.stylesheets, self.scripts = [], set(), [], [], [], []
        self.h1 = 0
        self.main = False
        self._label = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if tag == "h1":
            self.h1 += 1
        if tag == "main" and a.get("id") == "conteudo":
            self.main = True
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button"):
            self.controls.append((tag, a))
        if tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a.get("href"))
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])


def inventory(html):
    p = Inventory()
    p.feed(html)
    return p


class UiFlowBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in (patch.object(app, "DB_PATH", Path(self.temp.name) / "ui.db"), patch.object(app, "SEED_HISTORICO", False),
                  patch.object(app, "em_segundo_plano", lambda fn, *a: None),
                  patch.object(app, "route_conversation", side_effect=JevError("teste")),
                  patch.object(app, "llm", side_effect=RuntimeError("sem IA nos testes"))):
            p.start()
            self.addCleanup(p.stop)
        app.init_db()
        self.client = TestClient(app.app, follow_redirects=False)
        with app.db() as c:
            self.users = {u["nome"]: u["id"] for u in c.execute("SELECT id, nome FROM usuarios")}
            self.setor = c.execute("SELECT id FROM setores ORDER BY id LIMIT 1").fetchone()["id"]
        self.atendente = {"sess_painel": app.assinar(self.users["Carlos Lima"])}
        self.gestor = {"sess_painel": app.assinar(self.users["Paula Mendes"])}
        self.tecnico = {"sess_campo": app.assinar(self.users["Rafael Costa"])}
        self.outro = {"sess_campo": app.assinar(self.users["Diego Santos"])}

    def solicitar(self, **extra):
        dados = {"nome": "Ana Souza", "email": "ana.souza@exemplo.gov.br", "secretaria": app.SECRETARIAS[0],
                 "local": "UBS Vila Nova – Sala de vacinação", "contato": "2231", "titulo": "Sem internet",
                 "descricao": "Sem internet na sala de vacinação.", "tentativas": "reiniciei", "setor_id": self.setor,
                 "via_ia": 0} | extra
        r = self.client.post("/servidor/solicitar", data=dados)
        self.assertEqual(r.status_code, 303)
        token = r.headers["location"].split("/servidor/acompanhar/")[1].split("?")[0]
        with app.db() as c:
            return c.execute("SELECT * FROM solicitacoes WHERE token=?", (token,)).fetchone()

    def registrar(self, sol):
        r = self.client.post(f"/solicitacoes/{sol['id']}/registrar", cookies=self.atendente, data={
            "titulo": sol["titulo"], "descricao": sol["descricao"], "local": sol["local"], "setor_id": sol["setor_id"],
            "nome": sol["nome"], "email": sol["email"], "secretaria": sol["secretaria"]})
        self.assertEqual(r.status_code, 303)
        return int(re.search(r"/tarefa/(\d+)", r.headers["location"]).group(1))

    def tarefa(self, tid):
        with app.db() as c:
            return c.execute("SELECT * FROM tarefas WHERE id=?", (tid,)).fetchone()


class PaginasTests(UiFlowBase):
    def paginas(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        t = self.tarefa(tid)
        with app.db() as c:
            ex = c.execute("SELECT id FROM usuarios WHERE nome='Rafael Costa'").fetchone()["id"]
            c.execute("UPDATE tarefas SET executor_id=?, tipo_id=(SELECT id FROM tipos LIMIT 1) WHERE id=?", (ex, tid))
        return [("/login", None), ("/servidor", None), ("/servidor/solicitar", None), ("/meus-chamados", None),
                (f"/servidor/acompanhar/{sol['token']}", None), (f"/validar/{t['token']}", None),
                ("/atendente", self.atendente), (f"/tarefa/{tid}", self.atendente), ("/gestor", self.gestor),
                ("/gestor/metricas", self.gestor), ("/ranking", self.gestor), ("/config", self.gestor),
                ("/campo", self.tecnico), (f"/campo/{tid}", self.tecnico), ("/ranking", self.tecnico)]

    def test_todas_as_paginas_usam_somente_dataforge(self):
        for path, ck in self.paginas():
            with self.subTest(path=path):
                r = self.client.get(path, cookies=ck or {})
                self.assertEqual(r.status_code, 200, path)
                inv = inventory(r.text)
                self.assertEqual(inv.stylesheets, ["/static/ui/library.css", "/static/ui/app.css"])
                self.assertNotIn("pico", r.text.lower().replace("picos", ""))
                self.assertNotIn("/static/app.css", r.text)
                self.assertNotIn("lucide", r.text.lower())
                self.assertTrue(inv.main, "main#conteudo ausente")
                self.assertEqual(inv.h1, 1, "uma única h1 por página")
                for src in inv.scripts:
                    self.assertTrue(src.startswith("/static/"), src)

    def test_ids_unicos_e_campos_com_rotulo(self):
        for path, ck in self.paginas():
            with self.subTest(path=path):
                inv = inventory(self.client.get(path, cookies=ck or {}).text)
                dup = {i for i in inv.ids if inv.ids.count(i) > 1}
                self.assertFalse(dup, f"ids duplicados: {dup}")
                for tag, a in inv.controls:
                    if a.get("type") in ("radio", "checkbox"):
                        continue
                    self.assertTrue(a.get("id") in inv.labels_for or a.get("aria-label"), f"{tag} sem rótulo: {a}")

    def test_ids_usados_pelos_swaps_foram_preservados(self):
        esperados = {"/servidor": ["conversa", "form-chat", "digitando"], "/atendente": ["fila"], "/campo": ["lista", "btn-push", "banner"],
                     "/gestor": ["quadro"], "/gestor/metricas": ["analise"]}
        for path, ids in esperados.items():
            ck = {"/atendente": self.atendente, "/gestor": self.gestor, "/gestor/metricas": self.gestor, "/campo": self.tecnico}.get(path, {})
            html = self.client.get(path, cookies=ck).text
            for i in ids:
                self.assertIn(f'id="{i}"', html, f"{path}: #{i}")

    def test_navegacao_por_perfil(self):
        for ck, presentes, ausentes in ((self.atendente, ["/atendente", "/ranking"], ["/config", "/gestor/metricas"]),
                                        (self.gestor, ["/atendente", "/gestor", "/gestor/metricas", "/ranking", "/config"], []),
                                        (self.tecnico, ["/campo", "/ranking"], ["/atendente", "/gestor"])):
            path = "/ranking"
            html = self.client.get(path, cookies=ck).text
            nav = re.search(r'<nav class="df-nav".*?</nav>', html, re.S).group(0)
            for p in presentes:
                self.assertIn(f'href="{p}"', nav)
            for p in ausentes:
                self.assertNotIn(f'href="{p}"', nav)

    def test_tema_inicial_e_preferencia_persistida(self):
        html = self.client.get("/login").text
        self.assertIn("dataforge.theme", html)
        js = (Path(app.BASE) / "static/ui/app.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.setItem(KEY", js)
        self.assertIn("prefers-color-scheme", js)
        for escolha in ("light", "dark", "system"):
            self.assertIn(f'data-theme-choice="{escolha}"', html)


class FragmentosTests(UiFlowBase):
    def test_fragmentos_htmx_nao_trazem_pagina_inteira(self):
        sol = self.solicitar()
        self.registrar(sol)
        r = self.client.get("/atendente/fila", cookies=self.atendente)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<html", r.text)
        self.assertIn("df-item-card", r.text)
        r = self.client.post("/servidor/chat", data={"pergunta": "Estou sem internet"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<html", r.text)
        self.assertIn('id="digitando"', r.text)
        self.assertIn("df-bubble me", r.text)
        self.assertIn("Resolvido, obrigado!", r.text)
        self.assertIn("auto_token", r.headers.get("set-cookie", ""))

    def test_lista_do_campo_e_quadro(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        with app.db() as c:
            c.execute("UPDATE tarefas SET executor_id=?, status='encaminhado' WHERE id=?", (self.users["Rafael Costa"], tid))
        lista = self.client.get("/campo/lista", cookies=self.tecnico)
        self.assertIn(f'data-ultima="{tid}"', lista.text)
        self.assertIn("df-mission df-new", lista.text)
        quadro = self.client.get("/gestor/quadro", cookies=self.gestor)
        self.assertIn("df-board", quadro.text)
        self.assertEqual(self.client.get("/gestor/quadro?setor_id=0", cookies=self.gestor).status_code, 200)

    def test_polling_de_estado_continua_funcionando(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        t = self.tarefa(tid)
        r = self.client.get(f"/tarefa/{tid}/estado?v={t['atualizado_em']}", cookies=self.atendente)
        self.assertEqual(r.status_code, 204)
        self.assertNotIn("hx-refresh", {k.lower() for k in r.headers})
        r = self.client.get(f"/tarefa/{tid}/estado?v=antigo", cookies=self.atendente)
        self.assertEqual(r.headers.get("HX-Refresh"), "true")


class CaminhosTests(UiFlowBase):
    def test_helpdesk_resolve_e_servidor_valida(self):
        sol = self.solicitar(via_ia=0)
        self.assertEqual(sol["via_ia"], 0)
        self.assertTrue(sol["protocolo"].startswith("SOL-"))
        acomp = self.client.get(f"/servidor/acompanhar/{sol['token']}")
        self.assertIn("Aguardando atendente", acomp.text)
        self.assertIn(sol["protocolo"], acomp.text)
        tid = self.registrar(sol)
        r = self.client.post(f"/tarefa/{tid}/resolver-atendimento", cookies=self.atendente, data={"solucao": "Cabo reconectado por telefone."})
        self.assertEqual(r.status_code, 303)
        t = self.tarefa(tid)
        self.assertEqual(t["status"], "executado")
        self.assertEqual(t["resolvido_atendimento_por"], "Carlos Lima")
        pagina = self.client.get(f"/validar/{t['token']}")
        self.assertIn("O problema foi resolvido?", pagina.text)
        self.assertIn("Cabo reconectado", pagina.text)
        r = self.client.post(f"/validar/{t['token']}", data={"resultado": "resolvido", "nota": 5, "comentario": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "concluido")
        self.assertIn("Obrigado!", self.client.get(f"/validar/{t['token']}").text)

    def test_helpdesk_encaminha_tecnico_executa_e_servidor_valida(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        with app.db() as c:
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=?", (self.setor,)).fetchone()["id"]
        r = self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente,
                             data={"executor_id": self.users["Rafael Costa"], "tipo_id": tipo, "prioridade": "P2", "observacao": "Levar testador"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "encaminhado")
        self.assertIn("Aceitar e ir ao local", self.client.get(f"/campo/{tid}", cookies=self.tecnico).text)
        for para in ("a_caminho", "em_execucao"):
            self.assertEqual(self.client.post(f"/campo/{tid}/avancar", cookies=self.tecnico, data={"para": para}).status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "em_execucao")
        r = self.client.post(f"/campo/{tid}/concluir", cookies=self.tecnico, data={"relato": "Keystone trocado e testado."})
        self.assertEqual(r.status_code, 303)
        t = self.tarefa(tid)
        self.assertEqual(t["status"], "executado")  # execução técnica não fecha o chamado
        self.client.post(f"/validar/{t['token']}", data={"resultado": "resolvido", "nota": 4, "comentario": "Ok"})
        self.assertEqual(self.tarefa(tid)["status"], "concluido")

    def test_solicitacao_com_ia_e_acompanhamento_por_protocolo(self):
        r = self.client.post("/servidor/chat", data={"pergunta": "A impressora não imprime"})
        self.assertEqual(r.status_code, 200)
        cookies = {"auto_token": r.cookies.get("auto_token")}
        form = self.client.get("/servidor/solicitar?via=ia", cookies=cookies)
        self.assertIn("Preenchi os campos com base na nossa conversa", form.text)
        self.assertIn('name="via_ia" value="1"', form.text)
        sol = self.client.post("/servidor/solicitar", cookies=cookies, data={
            "nome": "Ana", "email": "ana@exemplo.gov.br", "secretaria": app.SECRETARIAS[1], "local": "Sala 2",
            "titulo": "Impressora", "descricao": "Não imprime", "setor_id": self.setor, "via_ia": 1})
        self.assertEqual(sol.status_code, 303)
        token = sol.headers["location"].split("/servidor/acompanhar/")[1].split("?")[0]
        with app.db() as c:
            row = c.execute("SELECT * FROM solicitacoes WHERE token=?", (token,)).fetchone()
        self.assertEqual(row["via_ia"], 1)
        self.assertIn("impressora", row["conversa"].lower())

    def test_meus_chamados_por_email_e_link(self):
        sol = self.solicitar(email="ana.souza@exemplo.gov.br")
        self.registrar(sol)
        self.assertIn("Receber link de acesso", self.client.get("/meus-chamados").text)
        r = self.client.post("/meus-chamados", data={"email": "ana.souza@exemplo.gov.br"})
        self.assertIn("E-mail simulado", r.text)
        link = re.search(r'href="(/meus-chamados/[^"]+)"', r.text).group(1)
        lista = self.client.get(link)
        self.assertEqual(lista.status_code, 200)
        self.assertIn('<h2 id="ab-h">Em aberto</h2>', lista.text)
        self.assertEqual(lista.text.count('class="df-open-card"'), 1)
        self.assertIn('id="meus"', lista.text)
        invalido = self.client.post("/meus-chamados", data={"email": "nao-e-email"})
        self.assertIn("e-mail válido", invalido.text.lower())
        self.assertEqual(self.client.get("/meus-chamados/inexistente").status_code, 404)

    def test_devolucao_impedimento_e_reabertura(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        with app.db() as c:
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=?", (self.setor,)).fetchone()["id"]
        atribuir = lambda: self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente,
                                            data={"executor_id": self.users["Rafael Costa"], "tipo_id": tipo, "prioridade": "P3"})
        atribuir()
        # devolução com motivo oficial volta à fila do atendimento
        r = self.client.post(f"/campo/{tid}/devolver", cookies=self.tecnico, data={"motivo": app.MOTIVOS_DEVOLUCAO[0], "detalhe": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "devolvido")
        self.assertIn("Devolvido", self.client.get(f"/tarefa/{tid}", cookies=self.atendente).text)
        atribuir()
        for para in ("a_caminho", "em_execucao"):
            self.client.post(f"/campo/{tid}/avancar", cookies=self.tecnico, data={"para": para})
        # impedimento (motivo externo) e resolução pelo gestor
        r = self.client.post(f"/campo/{tid}/impedimento", cookies=self.tecnico, data={"motivo": app.MOTIVOS_IMPEDIMENTO[0], "detalhe": "sem peça"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "impedido")
        with app.db() as c:
            iid = c.execute("SELECT id FROM impedimentos WHERE tarefa_id=?", (tid,)).fetchone()["id"]
        pagina = self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text
        self.assertIn("Impedimento aberto", pagina)
        self.assertIn("Resolver impedimento e liberar executor", pagina)
        self.assertEqual(self.client.post(f"/impedimento/{iid}/resolver", cookies=self.gestor, data={"providencia": "Peça enviada"}).status_code, 303)
        self.client.post(f"/campo/{tid}/concluir", cookies=self.tecnico, data={"relato": "Feito"})
        token = self.tarefa(tid)["token"]
        # reabertura: pendência com comentário obrigatório
        r = self.client.post(f"/validar/{token}", data={"resultado": "pendente", "nota": 2, "comentario": ""})
        self.assertIn("Conte o que ainda não está funcionando", unquote(r.headers["location"]))
        self.assertEqual(self.tarefa(tid)["status"], "executado")
        self.client.post(f"/validar/{token}", data={"resultado": "pendente", "nota": 2, "comentario": "Ainda sem rede"})
        self.assertEqual(self.tarefa(tid)["status"], "reaberto")
        self.assertIn("Recebemos sua pendência", self.client.get(f"/validar/{token}").text)

    def test_cancelamento_exige_motivo(self):
        tid = self.registrar(self.solicitar())
        r = self.client.post(f"/tarefa/{tid}/cancelar", cookies=self.atendente, data={"motivo": "  "})
        self.assertEqual(self.tarefa(tid)["status"], "novo")
        self.client.post(f"/tarefa/{tid}/cancelar", cookies=self.atendente, data={"motivo": "Duplicado"})
        self.assertEqual(self.tarefa(tid)["status"], "cancelado")
        pagina = self.client.get(f"/tarefa/{tid}", cookies=self.atendente).text
        self.assertIn("data-confirm=", self.client.get(f"/tarefa/{self.registrar(self.solicitar())}", cookies=self.atendente).text)
        self.assertIn("Cancelado", pagina)


class PermissoesETestadosTests(UiFlowBase):
    def test_permissoes_por_perfil(self):
        tid = self.registrar(self.solicitar())
        with app.db() as c:
            c.execute("UPDATE tarefas SET executor_id=? WHERE id=?", (self.users["Rafael Costa"], tid))
        self.assertEqual(self.client.get("/atendente", cookies=self.tecnico).status_code, 303)
        self.assertEqual(self.client.get("/gestor", cookies=self.atendente).status_code, 403)
        self.assertEqual(self.client.get("/config", cookies=self.atendente).status_code, 403)
        self.assertEqual(self.client.get(f"/campo/{tid}", cookies=self.outro).status_code, 403)
        self.assertEqual(self.client.get(f"/campo/{tid}", cookies=self.atendente).status_code, 303)

    def test_pagina_de_erro_e_permissao_negada(self):
        r = self.client.get("/gestor", cookies=self.atendente)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Acesso restrito", r.text)
        self.assertIn("/static/ui/app.css", r.text)
        self.assertNotIn("pico", r.text.lower())
        self.assertEqual(self.client.get("/nao-existe").status_code, 404)
        self.assertIn("Página não encontrada", self.client.get("/nao-existe").text)
        r = self.client.get("/validar/token-invalido")
        self.assertEqual(r.status_code, 404)
        api = self.client.get("/api/inexistente")
        self.assertEqual(api.headers["content-type"], "application/json")

    def test_sessao_expirada_avisa_no_login(self):
        r = self.client.get("/atendente", cookies={"sess_painel": "999.cookie-invalido"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login?msg=", r.headers["location"])
        self.assertIn("Sua sess", self.client.get(r.headers["location"]).text)
        self.assertEqual(self.client.get("/atendente").headers["location"], "/login")

    def test_validacao_de_formularios_mantem_contrato(self):
        r = self.client.post("/servidor/solicitar", data={"nome": "A", "email": "invalido", "secretaria": app.SECRETARIAS[0],
                             "local": "x", "titulo": "t", "descricao": "d", "setor_id": self.setor})
        self.assertEqual(r.status_code, 422)
        self.assertIn("e-mail válido", r.text)
        r = self.client.post("/servidor/solicitar", data={"nome": "A"})
        self.assertEqual(r.status_code, 422)

    def test_escape_de_conteudo_do_usuario(self):
        sol = self.solicitar(titulo="<script>alert(1)</script>", descricao="<img src=x onerror=alert(1)>")
        tid = self.registrar(sol)
        for path, ck in ((f"/tarefa/{tid}", self.atendente), ("/atendente", self.atendente)):
            html = self.client.get(path, cookies=ck).text
            self.assertNotIn("<script>alert(1)", html)
            self.assertNotIn("<img src=x", html)


class RecursosTests(unittest.TestCase):
    def test_arquivos_locais_do_frontend(self):
        base = Path(app.BASE)
        for rel in ("static/ui/library.css", "static/ui/app.css", "static/ui/app.js", "static/ui/library.js", "static/ui/phosphor.svg"):
            self.assertTrue((base / rel).exists(), rel)
        css = (base / "static/ui/app.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"https?://")
        js = (base / "static/ui/app.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, r"https?://")

    def test_icones_usados_existem_no_sprite(self):
        base = Path(app.BASE)
        sprite = (base / "static/ui/phosphor.svg").read_text(encoding="utf-8")
        existentes = set(re.findall(r'<symbol[^>]*id="([^"]+)"', sprite))
        usados = set()
        for f in (base / "templates/df").glob("*.html"):
            usados |= set(re.findall(r"icon\('([a-z0-9-]+)'\)", f.read_text(encoding="utf-8")))
            usados |= set(re.findall(r"glyph='([a-z0-9-]+)'", f.read_text(encoding="utf-8")))
        usados |= set(re.findall(r"'([a-z0-9-]+)'", re.search(r"STATUS_ICONES = \{(.*?)\}", (base / "templates/df/_ui.html").read_text(encoding="utf-8"), re.S).group(1)))
        faltando = {i for i in usados if i not in existentes and not i.startswith(("novo", "encaminhado", "a_caminho", "em_execucao", "impedido", "devolvido", "executado", "concluido", "reaberto", "cancelado"))}
        self.assertFalse(faltando, f"ícones fora do sprite: {faltando}")


class LegadoTests(unittest.TestCase):
    def test_legado_renderiza_com_biblioteca(self):
        import os
        os.environ.setdefault("LEGADO_DB", str(Path(tempfile.mkdtemp()) / "legado.db"))
        import legado
        c = TestClient(legado.app)
        for url in ("/", "/lista"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotIn("pico", r.text.lower(), url)


if __name__ == "__main__":
    unittest.main()
