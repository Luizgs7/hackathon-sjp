import app
from test_ui_flow import UiFlowBase


class GestorTecnicoTests(UiFlowBase):
    def test_distinct_profile_login_and_technical_permissions(self):
        login=self.client.get('/login')
        self.assertIn('Gestor do atendimento · Paula Mendes',login.text)
        self.assertIn('Gestor Suporte Técnico · Roberto Nunes',login.text)
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


class LoginDosTecnicosTests(UiFlowBase):
    def test_cada_tecnico_tem_perfil_na_area_do_seu_gestor(self):
        login = self.client.get('/login').text
        pares = {'Diego Santos': 'Suporte Técnico', 'Rafael Costa': 'Telecom', 'Marcos Vieira': 'Telefonia'}
        for tecnico, area in pares.items():
            self.assertIn(f'Técnico {area} · {tecnico}', login)
            with app.db() as c:
                area_id = c.execute("SELECT e.setor_id FROM usuarios u JOIN equipes e ON e.id=u.equipe_id WHERE u.nome=?", (tecnico,)).fetchone()[0]
                gestor = c.execute("SELECT nome FROM usuarios WHERE papeis='gestor_tecnico' AND area_id=?", (area_id,)).fetchone()
            self.assertIsNotNone(gestor, f'{area} sem gestor técnico')
            r = self.client.post('/login', data={'uid': self.users[tecnico]})
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers['location'], '/campo')
            self.assertEqual(self.client.get('/campo').status_code, 200)

    def test_missoes_de_exemplo_para_cada_tecnico_sem_orfas(self):
        with app.db() as c:
            app.semear_missoes_dos_tecnicos(c)
            app.semear_missoes_dos_tecnicos(c)  # idempotente
            orfas = c.execute("SELECT count(*) FROM tarefas WHERE executor_id IS NOT NULL AND executor_id NOT IN (SELECT id FROM usuarios)").fetchone()[0]
            self.assertEqual(orfas, 0)
            for nome in ('Diego Santos', 'Rafael Costa', 'Marcos Vieira'):
                estados = {r[0] for r in c.execute("SELECT t.status FROM tarefas t JOIN usuarios u ON u.id=t.executor_id WHERE u.nome=?", (nome,))}
                self.assertTrue({'encaminhado', 'a_caminho', 'executado'} <= estados, (nome, estados))
            for area in (2, 3, 5):
                self.assertEqual(c.execute("SELECT count(*) FROM tarefas WHERE status='devolvido' AND setor_id=?", (area,)).fetchone()[0], 1)
