import app
from test_ui_flow import UiFlowBase


class GestorTecnicoTests(UiFlowBase):
    def test_distinct_profile_login_and_technical_permissions(self):
        login=self.client.get('/login')
        self.assertIn('Gestor do atendimento · Paula Mendes',login.text)
        self.assertIn('Gestor técnico · Roberto Nunes',login.text)
        uid=self.users['Roberto Nunes']
        r=self.client.post('/login',data={'uid':uid})
        self.assertEqual(r.status_code,303)
        self.assertEqual(r.headers['location'],'/gestor')
        home=self.client.get('/gestor')
        self.assertIn('Gestão das equipes técnicas',home.text)
        self.assertNotIn('href="/atendente"',home.text)
        self.assertEqual(self.client.get('/gestor/quadro').status_code,200)
        self.assertEqual(self.client.get('/gestor/metricas').status_code,200)
        self.assertEqual(self.client.get('/ranking').status_code,200)
        self.assertEqual(self.client.get('/atendente').status_code,403)
        self.assertEqual(self.client.get('/config').status_code,403)
        self.assertEqual(self.client.post('/tarefas',data={'titulo':'Fictício','descricao':'Fictício','local':'Sala teste',
            'solicitante':'Ana Souza','email':app.SOLICITANTE_DEMO['email'],'secretaria':'Saúde','setor_id':self.setor}).status_code,403)

    def test_technical_manager_can_assign_but_cannot_resolve_at_helpdesk(self):
        sol=self.solicitar()
        tid=self.registrar(sol)
        with app.db() as c:  # Roberto responde pela área 2 (Suporte Técnico)
            tipo2=c.execute('SELECT id FROM tipos WHERE setor_id=2').fetchone()['id']
            c.execute('UPDATE tarefas SET setor_id=2 WHERE id=?',(tid,))
        self.client.post('/login',data={'uid':self.users['Roberto Nunes']})
        task=self.client.get(f'/tarefa/{tid}')
        self.assertEqual(task.status_code,200)
        self.assertNotIn(f'action="/tarefa/{tid}/resolver-atendimento"',task.text)
        self.assertNotIn('hx-get="/atendente/fila',task.text)
        self.assertEqual(self.client.post(f'/tarefa/{tid}/resolver-atendimento',data={'solucao':'Fictícia'}).status_code,403)
        r=self.client.post(f'/tarefa/{tid}/atribuir',data={'executor_id':self.users['Diego Santos'],'tipo_id':tipo2,'prioridade':'P3'})
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.tarefa(tid)['status'],'encaminhado')
        self.assertEqual(self.tarefa(tid)['executor_id'],self.users['Diego Santos'])
        self.assertEqual(self.client.post(f"/solicitacoes/{sol['id']}/descartar",data={'motivo':'Teste'}).status_code,403)

    def test_existing_database_upgrade_is_idempotent(self):
        with app.db() as c:
            for _ in range(3):
                app.garantir_gestor_tecnico(c)
            self.assertEqual(c.execute("SELECT count(*) FROM usuarios WHERE papeis='gestor_tecnico'").fetchone()[0],3)  # três gestores técnicos
            self.assertEqual(c.execute("SELECT papeis FROM usuarios WHERE nome='Paula Mendes'").fetchone()[0],'gestor,atendente')
