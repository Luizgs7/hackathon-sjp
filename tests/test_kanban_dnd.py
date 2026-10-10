"""Arrastar e soltar no quadro: o arrasto só abre a ação; as regras de transição continuam nos endpoints existentes."""
import app
from tests.test_ui_flow import UiFlowBase

FETCH = {"X-Requested-With": "fetch"}


class KanbanDndTests(UiFlowBase):
    def setUp(self):
        super().setUp()
        self.helena = {"sess_painel": app.assinar(self.users["Helena Prado"])}   # gestora técnica de Telecom (área 3)
        self.roberto = {"sess_painel": app.assinar(self.users["Roberto Nunes"])}  # gestor técnico de Suporte (área 2)
        self.tid = self.registrar(self.solicitar())
        with app.db() as c:
            c.execute("UPDATE tarefas SET setor_id=3 WHERE id=?", (self.tid,))
            self.tipo = c.execute("SELECT id FROM tipos WHERE setor_id=3").fetchone()["id"]

    def encaminhar_helena(self):
        """O Help Desk encaminha o chamado ao gestor técnico da Telecom, que passa a poder alocar o técnico."""
        r = self.client.post(f"/tarefa/{self.tid}/encaminhar-gt", cookies=self.gestor, data={"gestor_tecnico_id": self.users["Helena Prado"]})
        self.assertEqual(r.status_code, 303)

    def mover(self, para, cookies=None, tid=None):
        return self.client.get(f"/tarefa/{tid or self.tid}/mover", params={"para": para}, cookies=cookies or self.gestor)

    def test_alvos_por_perfil(self):
        self.assertEqual(app.alvos_arrasto({"papeis": "gestor,atendente"}, "novo"), {"encaminhado", "executado", "cancelado"})
        gt = {"id": 7, "papeis": "gestor_tecnico"}
        self.assertEqual(app.alvos_arrasto(gt, "novo"), {"cancelado"})                  # ainda não encaminhado pelo Help Desk
        self.assertEqual(app.alvos_arrasto(gt, "novo", 99), {"cancelado"})              # encaminhado a outro gestor técnico
        self.assertEqual(app.alvos_arrasto(gt, "novo", 7), {"encaminhado", "cancelado"})  # encaminhado a ele: aloca o técnico
        self.assertEqual(app.alvos_arrasto({"papeis": "gestor,atendente"}, "devolvido"), {"executado", "cancelado"})
        self.assertEqual(app.alvos_arrasto(gt, "devolvido"), {"encaminhado", "cancelado"})
        for s in ("encaminhado", "a_caminho", "em_execucao", "impedido"):
            self.assertEqual(app.alvos_arrasto({"papeis": "gestor"}, s), {"cancelado"}, s)
        for s in ("executado", "concluido", "cancelado"):
            self.assertEqual(app.alvos_arrasto({"papeis": "gestor"}, s), set(), s)

    def test_quadro_marca_cartoes_arrastaveis(self):
        html = self.client.get("/gestor/quadro", cookies=self.gestor).text
        self.assertIn('draggable="true"', html)
        self.assertIn('data-alvos="cancelado encaminhado executado"', html)
        self.assertIn('data-col="encaminhado"', html)
        self.assertIn("data-mover-abrir", html)
        self.assertIn('data-alvos="cancelado"', self.client.get("/gestor/quadro", cookies=self.helena).text)
        self.encaminhar_helena()
        self.assertIn('data-alvos="cancelado encaminhado"', self.client.get("/gestor/quadro", cookies=self.helena).text)

    def test_pagina_do_gestor_tem_modal_e_script(self):
        html = self.client.get("/gestor", cookies=self.gestor).text
        self.assertIn('id="modal-mover"', html)
        self.assertIn("/static/ui/quadro_dnd.js", html)

    def test_formularios_de_cada_destino(self):
        r = self.mover("encaminhado")  # gestora do Help Desk: escolhe 1 dos gestores técnicos, quem aloca é ele
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/tarefa/{self.tid}/encaminhar-gt", r.text)
        self.assertIn('name="gestor_tecnico_id"', r.text)
        self.assertNotIn('name="executor_id"', r.text)
        self.assertEqual(self.mover("encaminhado", cookies=self.helena).status_code, 409)  # antes do encaminhamento do Help Desk
        self.encaminhar_helena()
        r = self.mover("encaminhado", cookies=self.helena)  # gestor técnico: aloca o técnico da área
        self.assertIn(f"/tarefa/{self.tid}/atribuir", r.text)
        self.assertIn('name="executor_id"', r.text)
        self.assertIn(f"/tarefa/{self.tid}/resolver-atendimento", self.mover("executado").text)
        self.assertIn('name="motivo"', self.mover("cancelado").text)

    def test_destino_invalido_e_recusado(self):
        for para in ("concluido", "novo", "a_caminho", "inexistente", ""):
            self.assertEqual(self.mover(para).status_code, 409, para)

    def test_permissoes(self):
        self.assertEqual(self.mover("cancelado", cookies=self.atendente).status_code, 403)
        self.assertEqual(self.mover("cancelado", cookies=self.roberto).status_code, 403)   # chamado de outra área
        self.assertEqual(self.mover("cancelado", cookies=self.helena).status_code, 200)
        self.assertEqual(self.mover("executado", cookies=self.helena).status_code, 409)    # resolver é do Help Desk
        self.assertEqual(self.mover("cancelado", cookies={"sess_painel": "999.invalido"}).status_code, 303)

    def test_gestor_tecnico_ve_so_tecnicos_da_area(self):
        self.encaminhar_helena()
        html = self.mover("encaminhado", cookies=self.helena).text
        self.assertIn("Rafael Costa", html)
        self.assertNotIn("Diego Santos", html)

    def test_devolvido_so_o_gestor_tecnico_reencaminha(self):
        with app.db() as c:
            c.execute("UPDATE tarefas SET status='devolvido' WHERE id=?", (self.tid,))
        self.assertEqual(self.mover("encaminhado").status_code, 409)
        self.assertEqual(self.mover("encaminhado", cookies=self.helena).status_code, 200)

    def test_envio_do_modal_usa_os_endpoints_existentes(self):
        self.encaminhar_helena()
        r = self.client.post(f"/tarefa/{self.tid}/atribuir", cookies=self.helena, headers=FETCH, data={
            "executor_id": self.users["Rafael Costa"], "tipo_id": self.tipo, "prioridade": "P2", "observacao": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(self.tid)["status"], "encaminhado")
        self.assertEqual(app.alvos_arrasto({"papeis": "gestor"}, "encaminhado"), {"cancelado"})
        r = self.client.post(f"/tarefa/{self.tid}/cancelar", cookies=self.helena, headers=FETCH, data={"motivo": "Duplicado do #1"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(self.tid)["status"], "cancelado")
        self.assertEqual(self.mover("cancelado").status_code, 409)  # estado final não aceita nada

    def test_erro_volta_como_json_para_o_modal(self):
        self.encaminhar_helena()
        self.client.post(f"/tarefa/{self.tid}/atribuir", cookies=self.helena, data={
            "executor_id": self.users["Rafael Costa"], "tipo_id": self.tipo, "prioridade": "P2"})
        r = self.client.post(f"/tarefa/{self.tid}/resolver-atendimento", cookies=self.gestor, headers=FETCH, data={"solucao": "ok"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("erro", r.json())
