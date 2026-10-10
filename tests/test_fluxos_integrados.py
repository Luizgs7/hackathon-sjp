"""Os fluxos conversam entre si: o mesmo chamado atravessa solicitante, atendente, gestores, técnico e volta ao solicitante,
e a cada passo cada perfil enxerga e recebe o que lhe cabe. Roda para as 3 áreas (cada gestor técnico com o seu técnico)."""
import app
from test_ui_flow import UiFlowBase

AREAS = [("Suporte Técnico", "Roberto Nunes", "Diego Santos"),
         ("Telecom", "Helena Prado", "Rafael Costa"),
         ("Telefonia", "Camila Duarte", "Marcos Vieira")]


class FluxosIntegradosTests(UiFlowBase):
    def avisos(self, nome=None, email=None):
        dest = f"u:{self.users[nome]}" if nome else f"s:{email.lower()}"
        with app.db() as c:
            return [r["titulo"] for r in c.execute("SELECT titulo FROM notificacoes WHERE dest=? ORDER BY id", (dest,))]

    def cookie(self, nome, chave="sess_painel"):
        return {chave: app.assinar(self.users[nome])}

    def jornada(self, area, gestor, tecnico):
        with app.db() as c:
            area_id = c.execute("SELECT id FROM setores WHERE nome=?", (area,)).fetchone()["id"]
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=?", (area_id,)).fetchone()["id"]
        gt, tec = self.cookie(gestor), self.cookie(tecnico, "sess_campo")
        outro_gt = self.cookie(next(g for a, g, _ in AREAS if a != area))
        outro_tec = self.cookie(next(t for a, _, t in AREAS if a != area), "sess_campo")

        # 1) solicitante → atendente: a solicitação chega à fila e vira chamado
        sol = self.solicitar(setor_id=area_id)
        tid = self.registrar(sol)
        self.assertIn(f"#{tid}", self.client.get("/atendente/fila", cookies=self.atendente).text)
        email = self.tarefa(tid)["email"]

        # 2) atendente encaminha ao técnico da área: todos os envolvidos enxergam e são avisados
        r = self.alocar(tid, {
            "executor_id": self.users[tecnico], "tipo_id": tipo, "prioridade": "P2"})
        self.assertEqual(r.status_code, 303, area)
        self.assertEqual(self.tarefa(tid)["status"], "encaminhado")
        self.assertIn(f"Nova missão #{tid}", self.avisos(tecnico))                       # técnico
        self.assertTrue(any(f"#{tid}" in a for a in self.avisos(gestor)), area)          # gestor técnico da área
        self.assertTrue(any(f"#{tid}" in a for a in self.avisos(email=email)))           # solicitante
        self.assertIn(f"#{tid}", self.client.get("/gestor/quadro", cookies=self.gestor).text)   # gestora Help Desk
        self.assertIn(f"#{tid}", self.client.get("/gestor/quadro", cookies=gt).text)            # gestor da área
        self.assertNotIn(f"#{tid}", self.client.get("/gestor/quadro", cookies=outro_gt).text)   # gestor de outra área não vê
        self.assertEqual(self.client.get(f"/tarefa/{tid}", cookies=outro_gt).status_code, 403)
        self.assertIn(f'href="/campo/{tid}"', self.client.get("/campo/lista", cookies=tec).text)             # missão no celular do técnico
        self.assertNotIn(f'href="/campo/{tid}"', self.client.get("/campo/lista", cookies=outro_tec).text)    # técnico de outra área não vê
        self.assertIn(f"#{tid}", self.client.get("/gestor/equipe/painel", cookies=gt).text)     # carga da equipe

        # 3) técnico em campo: a trilha e o solicitante acompanham
        for para in ("a_caminho", "em_execucao"):
            self.assertEqual(self.client.post(f"/campo/{tid}/avancar", cookies=tec, data={"para": para}).status_code, 303)
        self.assertEqual(self.client.get(f"/api/rastro/{tid}", cookies=gt).status_code, 200)
        self.assertEqual(self.tarefa(tid)["status"], "em_execucao")
        self.assertIn("em execução", self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text.lower())

        # 4) execução → confirmação do solicitante → pontos do técnico e aviso à gestão
        self.client.post(f"/campo/{tid}/concluir", cookies=tec, data={"relato": "Equipamento reparado e testado."})
        self.assertEqual(self.tarefa(tid)["status"], "executado")
        token = self.tarefa(tid)["token"]
        r = self.client.post(f"/validar/{token}", data={"resultado": "resolvido", "nota": 5, "comentario": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid)["status"], "concluido")
        self.assertTrue(any(f"#{tid}" in a for a in self.avisos("Paula Mendes")))        # gestora Help Desk
        self.assertTrue(any("concluído" in a.lower() or "Concluído" in a for a in self.avisos(gestor)))
        self.assertEqual(self.client.get("/ranking", cookies=tec).status_code, 200)
        self.assertEqual(self.client.get("/ranking", cookies=gt).status_code, 200)
        self.assertEqual(self.client.get("/ranking", cookies=self.gestor).status_code, 403)      # sem gamificação no Help Desk
        with app.db() as c:
            self.assertGreater(c.execute("SELECT coalesce(sum(pontos),0) FROM pontos WHERE usuario_id=? AND tarefa_id=?",
                                         (self.users[tecnico], tid)).fetchone()[0], 0)

        # 5) segundo chamado: devolução pelo técnico → gestor da área reavalia e reencaminha
        tid2 = self.registrar(self.solicitar(setor_id=area_id, titulo="Outro problema", descricao="Segundo chamado do dia."))
        self.alocar(tid2, {
            "executor_id": self.users[tecnico], "tipo_id": tipo, "prioridade": "P3"})
        r = self.client.post(f"/campo/{tid2}/devolver", cookies=tec, data={"motivo": app.MOTIVOS_DEVOLUCAO[0], "detalhe": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid2)["status"], "devolvido")
        self.assertTrue(any(f"#{tid2}" in a and "Devolvido" in a for a in self.avisos(gestor)))
        self.assertTrue(any(f"#{tid2}" in a for a in self.avisos("Paula Mendes")))
        self.assertEqual(self.client.post(f"/tarefa/{tid2}/atribuir", cookies=self.atendente, data={
            "executor_id": self.users[tecnico], "tipo_id": tipo, "prioridade": "P3"}).status_code, 403)   # atendimento não aloca
        self.assertEqual(self.client.post(f"/tarefa/{tid2}/atribuir", cookies=gt, data={
            "executor_id": self.users[tecnico], "tipo_id": tipo, "prioridade": "P3"}).status_code, 303)  # gestor da área reencaminha
        self.assertEqual(self.tarefa(tid2)["status"], "encaminhado")

        # 6) cancelamento pela gestão: técnico, gestor da área e solicitante sabem
        r = self.client.post(f"/tarefa/{tid2}/cancelar", cookies=self.gestor, data={"motivo": "Duplicado"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.tarefa(tid2)["status"], "cancelado")
        self.assertTrue(any("Cancelado" in a and str(tid2) in a for a in self.avisos(tecnico)))
        self.assertTrue(any("Cancelado" in a and str(tid2) in a for a in self.avisos(gestor)))
        self.assertTrue(any("Cancelado" in a and str(tid2) in a for a in self.avisos(email=email)))

        # 7) reabertura pelo solicitante: volta à fila do atendimento e todos são avisados
        tid3 = self.registrar(self.solicitar(setor_id=area_id, titulo="Terceiro", descricao="Chamado para reabrir."))
        self.alocar(tid3, {
            "executor_id": self.users[tecnico], "tipo_id": tipo, "prioridade": "P3"})
        for para in ("a_caminho", "em_execucao"):
            self.client.post(f"/campo/{tid3}/avancar", cookies=tec, data={"para": para})
        self.client.post(f"/campo/{tid3}/concluir", cookies=tec, data={"relato": "Feito."})
        self.client.post(f"/validar/{self.tarefa(tid3)['token']}", data={"resultado": "pendente", "nota": 2, "comentario": "Voltou a falhar"})
        self.assertEqual(self.tarefa(tid3)["status"], "reaberto")
        self.assertIn(f"#{tid3}", self.client.get("/atendente/fila", cookies=self.atendente).text)
        for quem in ("Carlos Lima", "Paula Mendes", gestor, tecnico):
            self.assertTrue(any(str(tid3) in a for a in self.avisos(quem)), (area, quem))

    def test_jornada_completa_em_cada_area(self):
        for area, gestor, tecnico in AREAS:
            with self.subTest(area=area):
                self.jornada(area, gestor, tecnico)

    def test_todo_chamado_com_tecnico_aponta_para_perfil_existente(self):
        with app.db() as c:
            app.semear_missoes_dos_tecnicos(c)
            ids = {r["id"] for r in c.execute("SELECT id FROM usuarios")}
            for t in c.execute("SELECT id, executor_id, setor_id FROM tarefas WHERE executor_id IS NOT NULL"):
                self.assertIn(t["executor_id"], ids)
                area = c.execute("SELECT e.setor_id FROM usuarios u JOIN equipes e ON e.id=u.equipe_id WHERE u.id=?", (t["executor_id"],)).fetchone()[0]
                self.assertEqual(area, t["setor_id"], f"chamado {t['id']} com técnico de outra área")
                gestor = c.execute("SELECT count(*) FROM usuarios WHERE papeis='gestor_tecnico' AND area_id=?", (t["setor_id"],)).fetchone()[0]
                self.assertEqual(gestor, 1, f"área {t['setor_id']} sem gestor técnico")

    def test_helpdesk_encaminha_ao_gestor_tecnico_que_aloca_o_tecnico(self):
        with app.db() as c:
            area_id = c.execute("SELECT id FROM setores WHERE nome='Telecom'").fetchone()["id"]
            tipo = c.execute("SELECT id FROM tipos WHERE setor_id=?", (area_id,)).fetchone()["id"]
            gts = {r["nome"]: r["id"] for r in c.execute("SELECT nome, id FROM usuarios WHERE papeis='gestor_tecnico'")}
        tid = self.registrar(self.solicitar())
        helena, camila = self.cookie("Helena Prado"), self.cookie("Camila Duarte")
        pagina = self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text
        self.assertIn("Encaminhar ao gestor técnico", pagina)           # o botão existe para a gestora do Help Desk
        for nome in ("Roberto Nunes", "Helena Prado", "Camila Duarte"):  # e ela escolhe entre os 3 gestores técnicos
            self.assertIn(nome, pagina)
        # só gestor/atendente encaminham; gestor técnico e técnico não
        self.assertEqual(self.client.post(f"/tarefa/{tid}/encaminhar-gt", cookies=helena, data={"gestor_tecnico_id": gts["Helena Prado"]}).status_code, 403)
        r = self.client.post(f"/tarefa/{tid}/encaminhar-gt", cookies=self.gestor, data={
            "gestor_tecnico_id": gts["Helena Prado"], "prioridade": "P2", "observacao": "Urgente para a UBS"})
        self.assertEqual(r.status_code, 303)
        t = self.tarefa(tid)
        self.assertEqual((t["status"], t["setor_id"], t["gt_destino_id"], t["prioridade"]), ("novo", area_id, gts["Helena Prado"], "P2"))
        self.assertTrue(any(f"#{tid} aguarda alocação" in a for a in self.avisos("Helena Prado")))
        self.assertFalse(any(f"#{tid}" in a for a in self.avisos("Camila Duarte")))
        # sai da fila do atendimento; o gestor da área enxerga e a de outra área não
        self.assertNotIn(f"#{tid}", self.client.get("/atendente/fila", cookies=self.atendente).text)
        self.assertIn("aguardando gestor técnico", self.client.get("/gestor/quadro", cookies=helena).text)
        self.assertNotIn(f"#{tid}", self.client.get("/gestor/quadro", cookies=camila).text)
        self.assertIn("Alocar técnico", self.client.get(f"/tarefa/{tid}", cookies=helena).text)
        self.assertIn("Aguardando o gestor técnico", self.client.get(f"/tarefa/{tid}", cookies=self.gestor).text)
        # o gestor técnico aloca o técnico da área; o chamado passa a Encaminhado e o técnico é avisado
        r = self.client.post(f"/tarefa/{tid}/atribuir", cookies=helena, data={
            "executor_id": self.users["Rafael Costa"], "tipo_id": tipo, "prioridade": "P2"})
        self.assertEqual(r.status_code, 303)
        t = self.tarefa(tid)
        self.assertEqual((t["status"], t["executor_id"], t["gt_destino_id"]), ("encaminhado", self.users["Rafael Costa"], None))
        self.assertIn(f"Nova missão #{tid}", self.avisos("Rafael Costa"))
        # chamado já encaminhado não pode ser reencaminhado ao gestor técnico
        self.assertEqual(self.client.post(f"/tarefa/{tid}/encaminhar-gt", cookies=self.gestor, data={"gestor_tecnico_id": gts["Camila Duarte"]}).status_code, 409)
