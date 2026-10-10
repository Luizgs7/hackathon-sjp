"""Fluxo completo na interface DataForge: páginas, fragmentos htmx, permissões e caminhos de atendimento.

Usa banco SQLite temporário, dados fictícios e respostas simuladas de IA (nenhuma chamada externa)."""
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

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
                self.assertEqual([urlsplit(url).path for url in inv.stylesheets], ["/static/ui/library.css", "/static/ui/app.css"])
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
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=3").fetchone()["id"]  # área do Rafael (Telecom)
        helena = {"sess_painel": app.assinar(self.users["Helena Prado"])}  # gestora técnica de Telecom
        atribuir = lambda ck: self.client.post(f"/tarefa/{tid}/atribuir", cookies=ck,
                                               data={"executor_id": self.users["Rafael Costa"], "tipo_id": tipo, "prioridade": "P3"})
        atribuir(self.atendente)
        # devolução com motivo oficial vai ao gestor técnico da área (não à fila do atendimento)
        r = self.client.post(f"/campo/{tid}/devolver", cookies=self.tecnico, data={"motivo": app.MOTIVOS_DEVOLUCAO[0], "detalhe": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "devolvido")
        self.assertIn("Devolvido ao gestor técnico", self.client.get(f"/tarefa/{tid}", cookies=self.atendente).text)
        self.assertNotIn(f"#{tid}", self.client.get("/atendente/fila", cookies=self.atendente).text)
        self.assertEqual(atribuir(self.atendente).status_code, 403)  # o atendimento não reavalia
        self.assertIn("Reavaliar chamado devolvido", self.client.get(f"/tarefa/{tid}", cookies=helena).text)
        self.assertEqual(atribuir(helena).status_code, 303)
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


class RastroTecnicoTests(unittest.TestCase):
    def test_posicao_simulada_avanca_e_chega(self):
        from datetime import datetime, timedelta
        import rastro
        ini = "2026-10-10 10:00:00"
        t0 = datetime(2026, 10, 10, 10, 0, 0)
        a = rastro.estado(7, "a_caminho", ini, t0)
        b = rastro.estado(7, "a_caminho", ini, t0 + timedelta(seconds=180))
        c = rastro.estado(7, "a_caminho", ini, t0 + timedelta(hours=1))
        self.assertEqual(a["posicao"], list(rastro.BASE))
        self.assertNotEqual(a["posicao"], b["posicao"])
        self.assertTrue(c["chegou"])
        self.assertEqual(c["posicao"], c["destino"])
        self.assertTrue(rastro.estado(7, "em_execucao", ini)["chegou"])
        self.assertIsNone(rastro.estado(7, "encaminhado", ini))
        self.assertEqual(rastro.destino(7), rastro.destino(7))


class RastroApiTests(UiFlowBase):
    def test_acesso_ao_mapa_por_perfil(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        with app.db() as c:
            c.execute("UPDATE tarefas SET executor_id=? WHERE id=?", (self.users["Rafael Costa"], tid))
        self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.gestor, data={
            "executor_id": self.users["Rafael Costa"], "tipo_id": 1, "prioridade": "P2"})
        self.assertEqual(self.client.get(f"/api/rastro/{tid}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}", cookies=self.atendente).status_code, 403)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}", cookies=self.gestor).json()["ativo"], False)
        self.client.post(f"/campo/{tid}/avancar", cookies=self.tecnico, data={"para": "a_caminho"})
        d = self.client.get(f"/api/rastro/{tid}", cookies=self.gestor).json()
        self.assertTrue(d["simulado"] and len(d["posicao"]) == 2)
        t = self.tarefa(tid)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}?token={t['token']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}?token={sol['token']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}?token=errado").status_code, 403)
        self.assertIn("mapa-tecnico", self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text)
        self.assertNotIn("mapa-tecnico", self.client.get(f"/tarefa/{tid}", cookies=self.atendente).text)
        self.assertIn("mapa-tecnico", self.client.get(f"/validar/{t['token']}").text)
        self.assertIn("mapa-tecnico", self.client.get(f"/servidor/acompanhar/{sol['token']}").text)


class ChatComAtendenteTests(UiFlowBase):
    def pedir_atendente(self):
        r = self.client.post("/servidor/chat", data={"pergunta": "Meu computador não liga"})
        self.assertEqual(r.status_code, 200)
        tok = self.client.cookies.get("auto_token")
        r = self.client.post("/servidor/atendente", data={"estado_chat": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Aguardando um atendente", r.text)
        return tok

    def test_fluxo_completo_solicitante_e_atendente(self):
        tok = self.pedir_atendente()
        fila = self.client.get("/atendente/conversas", cookies=self.atendente)
        self.assertIn("Meu computador não liga", fila.text)
        self.assertIn("Aguardando atendente", fila.text)
        # o assistente não responde enquanto aguarda
        r = self.client.post("/servidor/chat", data={"pergunta": "Alguém aí?"})
        with app.db() as c:
            autores = [m["autor"] for m in c.execute("SELECT autor FROM auto_msgs WHERE token=? ORDER BY id", (tok,))]
        self.assertEqual(autores.count("assistente"), 1)  # só a resposta anterior ao pedido
        # só atendente/gestor
        self.assertEqual(self.client.get(f"/atendente/conversa/{tok}", cookies=self.tecnico).status_code, 303)
        self.assertEqual(self.client.post(f"/atendente/conversa/{tok}/responder", data={"texto": "x"},
                                          cookies=self.atendente).status_code, 409)
        self.assertEqual(self.client.post(f"/atendente/conversa/{tok}/assumir", cookies=self.atendente).status_code, 303)
        # outro atendente não toma a conversa
        self.assertEqual(self.client.post(f"/atendente/conversa/{tok}/assumir", cookies=self.gestor).status_code, 409)
        self.client.post(f"/atendente/conversa/{tok}/responder", data={"texto": "Olá, vou ajudar."}, cookies=self.atendente)
        r = self.client.get("/servidor/mensagens")
        self.assertIn("Olá, vou ajudar.", r.text)
        self.assertIn("Carlos Lima", r.text)
        # abrir chamado a partir da conversa
        r = self.client.post(f"/atendente/conversa/{tok}/chamado", cookies=self.atendente)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/atendente?sol=", r.headers["location"])
        self.assertEqual(self.client.get("/atendente/conversas", cookies=self.atendente).text.count("df-list-item"), 0)
        r = self.client.get("/servidor/mensagens")
        self.assertIn("abriu a solicitação", r.text)

    def test_devolver_e_encerrar(self):
        tok = self.pedir_atendente()
        self.client.post(f"/atendente/conversa/{tok}/assumir", cookies=self.atendente)
        self.client.post(f"/atendente/conversa/{tok}/devolver", cookies=self.atendente)
        with app.db() as c:
            self.assertEqual(c.execute("SELECT modo FROM autoatendimento WHERE token=?", (tok,)).fetchone()["modo"], "ia")
        self.client.post(f"/atendente/conversa/{tok}/assumir", cookies=self.atendente)
        self.client.post(f"/atendente/conversa/{tok}/encerrar", cookies=self.atendente)
        with app.db() as c:
            self.assertEqual(c.execute("SELECT resolvido FROM autoatendimento WHERE token=?", (tok,)).fetchone()["resolvido"], 1)


class DisponibilidadeEKmTests(UiFlowBase):
    def encaminhar(self, executor="Rafael Costa"):
        tid = self.registrar(self.solicitar())
        r = self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente, data={
            "executor_id": self.users[executor], "tipo_id": 1, "prioridade": "P2"})
        return tid, r

    def test_tecnico_inativo_nao_recebe_missao(self):
        self.client.post("/campo/disponibilidade", cookies=self.tecnico, data={"ativo": 0})
        with app.db() as c:
            ids = [e["id"] for e in app.carga_executores(c)]
        self.assertNotIn(self.users["Rafael Costa"], ids)
        tid, r = self.encaminhar()
        self.assertEqual(r.status_code, 409)
        self.assertIn("Inativo", self.client.get("/campo", cookies=self.tecnico).text)
        self.client.post("/campo/disponibilidade", cookies=self.tecnico, data={"ativo": 1})
        tid, r = self.encaminhar()
        self.assertEqual(r.status_code, 303)

    def test_km_contabilizado_e_visivel_ao_gestor(self):
        tid, _ = self.encaminhar()
        self.client.post(f"/campo/{tid}/avancar", cookies=self.tecnico, data={"para": "a_caminho"})
        self.assertEqual(self.tarefa(tid)["km_percorrido"], 0)
        self.client.post(f"/campo/{tid}/avancar", cookies=self.tecnico, data={"para": "em_execucao"})
        km = self.tarefa(tid)["km_percorrido"]
        self.assertGreater(km, 0.5)
        self.assertIn("km até o local", self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text)
        pag = self.client.get("/gestor/metricas", cookies=self.gestor).text
        self.assertIn("Km rodados", pag)
        self.assertIn("Rafael Costa", pag)


class PainelEquipeTests(UiFlowBase):
    def setUp(self):
        super().setUp()
        with app.db() as c:
            app.garantir_gestor_tecnico(c)
            gt = c.execute("SELECT id FROM usuarios WHERE papeis='gestor_tecnico'").fetchone()["id"]
        self.gt = {"sess_painel": app.assinar(gt)}

    def novo(self, local, executor=None):
        tid = self.registrar(self.solicitar(local=local))
        if executor:
            self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente, data={
                "executor_id": self.users[executor], "tipo_id": 1, "prioridade": "P2"})
        return tid

    def test_acesso_e_conteudo(self):
        self.assertEqual(self.client.get("/gestor/equipe", cookies=self.atendente).status_code, 403)
        self.assertEqual(self.client.get("/gestor/equipe", cookies=self.tecnico).status_code, 303)
        self.assertEqual(self.client.get("/gestor/equipe", cookies=self.gt).status_code, 200)
        a = self.novo("UBS Centro", "Rafael Costa")
        self.novo("UBS Centro", "Rafael Costa")
        self.client.post(f"/campo/{a}/avancar", cookies=self.tecnico, data={"para": "a_caminho"})
        self.client.post(f"/campo/{a}/avancar", cookies=self.tecnico, data={"para": "em_execucao"})
        pend = self.novo("Escola Municipal Sul")
        pagina = self.client.get("/gestor/equipe/painel", cookies=self.gestor).text
        self.assertIn("chamado(s) no backlog", pagina)
        self.assertIn("Rafael Costa", pagina)
        self.assertIn("Em atendimento agora", pagina)
        self.assertIn(f"#{a}", pagina)
        self.assertIn("UBS Centro", pagina)
        self.assertIn(f"#{pend}", pagina)
        self.assertIn("Direcionar", pagina)

    def test_ranking_por_distancia_e_inativo_fora(self):
        a = self.novo("UBS Centro", "Rafael Costa")
        self.client.post(f"/campo/{a}/avancar", cookies=self.tecnico, data={"para": "a_caminho"})
        self.client.post(f"/campo/{a}/avancar", cookies=self.tecnico, data={"para": "em_execucao"})
        self.novo("UBS Centro")
        with app.db() as c:
            d = app.painel_equipe(c)
        rank = d["pendentes"][0]["ranking"]
        self.assertEqual(rank[0]["nome"], "Rafael Costa")  # já está no mesmo local: 0 km
        self.assertEqual(rank[0]["km"], 0)
        self.client.post("/campo/disponibilidade", cookies=self.tecnico, data={"ativo": 0})
        with app.db() as c:
            nomes = [r["nome"] for r in app.painel_equipe(c)["pendentes"][0]["ranking"]]
        self.assertNotIn("Rafael Costa", nomes)

    def test_dados_do_mapa(self):
        self.novo("UBS Centro")
        r = self.client.get("/gestor/equipe/dados", cookies=self.gestor)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(len(j["pendentes"]), 1)
        self.assertTrue(j["tecnicos"] and len(j["tecnicos"][0]["posicao"]) == 2)
        self.assertEqual(self.client.get("/gestor/equipe/dados", cookies=self.atendente).status_code, 403)


class BuscaPushEManifestTests(UiFlowBase):
    def test_busca_no_quadro_somente_em_aberto(self):
        a = self.registrar(self.solicitar(titulo="Impressora quebrada", descricao="Papel preso na bandeja"))
        b = self.registrar(self.solicitar(titulo="Sem internet", descricao="Rede caiu na recepção"))
        self.client.post(f"/tarefa/{b}/cancelar", cookies=self.atendente, data={"motivo": "duplicado"})
        quadro = lambda q: self.client.get("/gestor/quadro", params={"q": q}, cookies=self.gestor).text
        self.assertIn("Impressora quebrada", quadro("impressora"))
        self.assertNotIn("Sem internet", quadro("impressora"))
        self.assertIn("Impressora quebrada", quadro(f"#{a}"))
        self.assertIn("Impressora quebrada", quadro("bandeja papel"))  # vários termos
        self.assertIn("Impressora quebrada", quadro("IMPRESSORA"))      # sem diferenciar maiúsculas
        self.assertIn("0 chamado(s) em aberto", quadro("rede caiu"))      # cancelado não entra
        self.assertIn("Sem internet", self.client.get("/gestor/quadro", cookies=self.gestor).text)

    def test_manifest_e_push_do_tecnico(self):
        m = self.client.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        self.assertTrue(any(i["sizes"] == "192x192" for i in m.json()["icons"]))
        self.assertIn('rel="manifest"', self.client.get("/login").text)
        self.assertEqual(self.client.get("/static/ui/pwa/icone-192.png").status_code, 200)
        self.assertEqual(self.client.get("/sw.js").status_code, 200)
        chave = self.client.get("/api/push/chave").json()["chave"]
        self.assertGreater(len(chave), 80)
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
        self.assertEqual(self.client.post("/api/push/inscrever", json=sub).status_code, 303)  # exige sessão do técnico
        self.assertEqual(self.client.post("/api/push/inscrever", json=sub, cookies=self.tecnico).status_code, 200)

    def test_push_enviado_ao_encaminhar(self):
        enviados = []
        with patch.object(app, "enviar_push", lambda *a: enviados.append(a)), \
                patch.object(app, "em_segundo_plano", lambda fn, *a: fn(*a)):
            tid = self.registrar(self.solicitar())
            self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente, data={
                "executor_id": self.users["Rafael Costa"], "tipo_id": 1, "prioridade": "P2"})
        pushes = [e for e in enviados if e[0] == self.users["Rafael Costa"]]
        self.assertTrue(pushes)
        self.assertEqual(pushes[0][3], f"/campo/{tid}")


class EvidenciaEBotaoTests(UiFlowBase):
    def test_evidencia_ausente_mostra_placeholder(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        t = self.tarefa(tid)
        with app.db() as c:
            c.execute("UPDATE tarefas SET evidencia='nao_existe.jpg', status='executado', executor_id=? WHERE id=?",
                      (self.users["Rafael Costa"], tid))
        for url, ck in ((f"/validar/{t['token']}", None), (f"/tarefa/{tid}", self.gestor)):
            r = self.client.get(url, cookies=ck or {})
            self.assertNotIn("/uploads/nao_existe.jpg", r.text, url)
            self.assertIn("df-empty", r.text, url)

    def test_evidencia_existente_mostra_imagem(self):
        sol = self.solicitar()
        tid = self.registrar(sol)
        t = self.tarefa(tid)
        (app.UPLOADS / "evid_teste.jpg").write_bytes(b"\xff\xd8\xff")
        self.addCleanup(lambda: (app.UPLOADS / "evid_teste.jpg").unlink(missing_ok=True))
        with app.db() as c:
            c.execute("UPDATE tarefas SET evidencia='evid_teste.jpg', status='executado', executor_id=? WHERE id=?",
                      (self.users["Rafael Costa"], tid))
        self.assertIn("/uploads/evid_teste.jpg", self.client.get(f"/validar/{t['token']}").text)

    def test_botao_falar_com_atendente_no_chat_vazio(self):
        r = self.client.get("/servidor")
        self.assertIn("Falar com atendente", r.text)
        self.assertIn('id="acoes-vazio"', r.text)
        self.assertIn("/servidor/atendente", r.text)


class AreaDoGestorTecnicoTests(UiFlowBase):
    def setUp(self):
        super().setUp()
        self.helena = {"sess_painel": app.assinar(self.users["Helena Prado"])}   # Telecom (área 3)
        self.roberto = {"sess_painel": app.assinar(self.users["Roberto Nunes"])}  # Suporte (área 2)

    def chamado(self, setor, titulo="Sem rede"):
        tid = self.registrar(self.solicitar(titulo=titulo))
        with app.db() as c:
            c.execute("UPDATE tarefas SET setor_id=? WHERE id=?", (setor, tid))
        return tid

    def test_gestor_tecnico_ve_apenas_a_sua_area(self):
        a = self.chamado(3, "Chamado de Telecom")
        b = self.chamado(2, "Chamado de Suporte")
        quadro = self.client.get("/gestor/quadro", cookies=self.helena).text
        self.assertIn("Chamado de Telecom", quadro)
        self.assertNotIn("Chamado de Suporte", quadro)
        # tenta forçar outra área pelo parâmetro: continua só a sua
        self.assertNotIn("Chamado de Suporte", self.client.get("/gestor/quadro", params={"setor_id": 2}, cookies=self.helena).text)
        self.assertEqual(self.client.get(f"/tarefa/{a}", cookies=self.helena).status_code, 200)
        self.assertEqual(self.client.get(f"/tarefa/{b}", cookies=self.helena).status_code, 403)
        self.assertEqual(self.client.post(f"/tarefa/{b}/cancelar", data={"motivo": "x"}, cookies=self.helena).status_code, 403)
        # gestor do Help Desk continua vendo tudo
        todos = self.client.get("/gestor/quadro", cookies=self.gestor).text
        self.assertIn("Chamado de Telecom", todos)
        self.assertIn("Chamado de Suporte", todos)

    def test_devolvido_so_para_o_gestor_tecnico_da_area_com_cancelamento_justificado(self):
        tid = self.chamado(3)
        with app.db() as c:
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=3").fetchone()["id"]
        self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.atendente, data={
            "executor_id": self.users["Rafael Costa"], "tipo_id": tipo, "prioridade": "P3"})
        self.client.post(f"/campo/{tid}/devolver", cookies=self.tecnico, data={"motivo": app.MOTIVOS_DEVOLUCAO[1], "detalhe": ""})
        # aparece para o gestor técnico da área e não para o de outra área
        self.assertIn(f"#{tid}", self.client.get("/gestor/equipe/painel", cookies=self.helena).text)
        self.assertNotIn(f"#{tid}", self.client.get("/gestor/equipe/painel", cookies=self.roberto).text)
        # direcionar a técnico de outra área é recusado
        r = self.client.post(f"/tarefa/{tid}/atribuir", cookies=self.helena, data={
            "executor_id": self.users["Diego Santos"], "tipo_id": tipo, "prioridade": "P3"})
        self.assertEqual(r.status_code, 403)
        # cancelar exige justificativa
        r = self.client.post(f"/tarefa/{tid}/cancelar", cookies=self.helena, data={"motivo": "  "})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "devolvido")
        self.client.post(f"/tarefa/{tid}/cancelar", cookies=self.helena, data={"motivo": "Chamado duplicado"})
        self.assertEqual(self.tarefa(tid)["status"], "cancelado")


class BancoNovoComHistoricoTests(unittest.TestCase):
    def test_inicializacao_com_historico_e_gestores_por_area(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "DB_PATH", Path(d) / "novo.db"), \
                patch.object(app, "SEED_HISTORICO", True):
            app.init_db()
            with app.db() as c:
                self.assertGreater(c.execute("SELECT count(*) FROM tarefas").fetchone()[0], 100)
                areas = {r["area_id"] for r in c.execute("SELECT area_id FROM usuarios WHERE papeis='gestor_tecnico'")}
                self.assertEqual(areas, {2, 3, 4, 5, 6})
                self.assertTrue(all(e["setor_id"] for e in app.carga_executores(c)))
