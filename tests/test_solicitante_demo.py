from datetime import datetime, timedelta
import re
from unittest.mock import patch

import app
from test_ui_flow import UiFlowBase


class SolicitanteDemoTests(UiFlowBase):
    def test_missing_demo_access_is_restored_but_tampered_identity_is_rejected(self):
        self.client.post('/login/solicitante')
        with app.db() as c:
            c.execute('DELETE FROM acessos_solicitante')
        self.assertIn('value="Ana Souza"',self.client.get('/servidor/solicitar').text)
        self.assertEqual(self.client.get('/meus-chamados').status_code,303)
        signed = self.client.cookies.get('solicitante_demo')
        self.client.cookies.set('solicitante_demo',signed[:-1]+'x')
        self.assertNotIn('value="Ana Souza"',self.client.get('/servidor/solicitar').text)

    def test_recipe_is_refused_without_calls_or_ticket_actions(self):
        with patch.object(app,'route_conversation') as jev, patch.object(app,'llm') as llm:
            r = self.client.post('/servidor/chat',data={'pergunta':'Me passa uma receita de bolo de chocolate'})
            jev.assert_not_called(); llm.assert_not_called()
        self.assertIn('Não posso ajudar com esse assunto',r.text)
        self.assertNotIn('Não resolveu: abrir solicitação',r.text)
        self.assertNotIn('Resolvido, obrigado!',r.text)
        self.assertEqual(self.client.post('/servidor/resolvido').status_code,409)
        self.assertEqual(self.client.get('/servidor/solicitar?via=ia').status_code,409)
        with app.db() as c:
            self.assertEqual(app.contar_auto_resolvidos(c),0)
            self.assertEqual(c.execute('SELECT count(*) FROM solicitacoes').fetchone()[0],0)

    def test_linked_conversation_keeps_history_and_reports_live_status(self):
        self.client.post('/login/solicitante')
        self.client.post('/servidor/chat',data={'pergunta':'Impressora de teste sem conexão'})
        s = self.solicitar(email=app.SOLICITANTE_DEMO['email'],via_ia=1)
        home = self.client.get('/servidor')
        self.assertIn('Impressora de teste sem conexão',home.text)
        self.assertIn(s['protocolo'],home.text)
        self.assertNotIn('Não resolveu: abrir solicitação',home.text)
        tid = self.registrar(s)
        with app.db() as c:
            c.execute("UPDATE tarefas SET status='executado' WHERE id=?",(tid,))
        with patch.object(app,'llm') as llm:
            answer=self.client.post('/servidor/chat',data={'pergunta':'Qual o status do chamado?'})
            llm.assert_not_called()
        self.assertIn(app.STATUS['executado'][1],answer.text)
        self.assertIn('Impressora de teste sem conexão',answer.text)
        self.assertIn(app.STATUS['executado'][1],self.client.get('/servidor/status').text)
        dashboard=self.client.get('/servidor/dashboard')
        self.assertEqual(dashboard.status_code,200)
        self.assertIn(s['protocolo'],dashboard.text)
        with app.db() as c:
            token=c.execute('SELECT token FROM autoatendimento WHERE solicitacao_id=?',(s['id'],)).fetchone()[0]
            self.assertEqual(c.execute('SELECT count(*) FROM solicitacoes').fetchone()[0],1)
        self.client.post('/servidor/nova')
        self.assertEqual(self.client.get('/servidor/conversas/'+token).status_code,303)
        self.assertIn('Impressora de teste sem conexão',self.client.get('/servidor').text)
        self.client.get('/meus-chamados-sair')
        self.assertEqual(self.client.get('/servidor/conversas/'+token).status_code,404)
        self.assertEqual(self.client.get('/servidor/dashboard').status_code,303)

    def test_no_ai_form_starts_tracking_conversation_and_preserves_identity(self):
        self.client.post('/login/solicitante')
        response=self.client.post('/servidor/solicitar',data={'nome':'Ana Souza','email':app.SOLICITANTE_DEMO['email'],
            'secretaria':'Saúde','local':'Sala fictícia','titulo':'Ramal de teste','descricao':'Ramal sem linha',
            'setor_id':self.setor,'continuar_chat':1})
        self.assertTrue(response.headers['location'].startswith('/servidor?'))
        chat=self.client.get('/servidor')
        self.assertIn('Ramal sem linha',chat.text)
        self.assertIn('Ana Souza',chat.text)
        self.assertIn('SOL-0001',chat.text)
        dashboard=self.client.get('/servidor/dashboard')
        self.assertIn('Ramal sem linha',dashboard.text)

    def test_resolved_chat_is_readable_without_ticket_and_counted_once(self):
        with app.db() as c:
            before_tasks = c.execute('SELECT count(*) FROM tarefas').fetchone()[0]
        with patch.object(app, 'responder_autoatendimento', return_value=('Orientação fictícia de IA', 'llm')):
            chat = self.client.post('/servidor/chat', data={'pergunta': 'Como imprimir uma página de teste?'})
        state = re.search(r'id="estado-chat" name="estado_chat" value="([^"]+)"', chat.text)[1]
        with app.db() as c:
            c.execute('DELETE FROM auto_msgs')
            c.execute('DELETE FROM autoatendimento')
        resolved = self.client.post('/servidor/resolvido', data={'estado_chat': state})
        self.assertEqual(resolved.status_code, 200)
        self.assertIn('Como imprimir uma página de teste?', resolved.text)
        self.assertIn('Resolvida pelo assistente · sem chamado', resolved.text)
        self.assertNotIn('id="form-chat"', resolved.text)
        closed = re.search(r'id="estado-chat" name="estado_chat" value="([^"]+)"', resolved.text)[1]
        with app.db() as c:
            self.assertEqual(app.contar_auto_resolvidos(c, somente_ia=True), 1)
            self.assertEqual(c.execute('SELECT count(*) FROM solicitacoes').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM tarefas').fetchone()[0], before_tasks)
        for _ in range(2):
            history = self.client.post('/servidor/historico', data={'estado_chat': closed})
            self.assertEqual(history.status_code, 200)
            self.assertIn('Orientação fictícia de IA', history.text)
        self.client.post('/servidor/resolvido', data={'estado_chat': closed})
        with app.db() as c:
            self.assertEqual(app.contar_auto_resolvidos(c, somente_ia=True), 1)
            c.execute('DELETE FROM auto_msgs')
            c.execute('DELETE FROM autoatendimento')
        # Histórico continua consultável mesmo após a instância perder o SQLite.
        self.assertIn('Orientação fictícia de IA', self.client.post('/servidor/historico', data={'estado_chat': closed}).text)
        with app.db() as c:
            self.assertEqual(app.contar_auto_resolvidos(c), 0)
        self.assertEqual(self.client.post('/servidor/historico', data={'estado_chat': state}).status_code, 400)

    def test_metric_separates_ai_and_fallback_resolutions(self):
        for source in ('llm', 'jev', 'regras'):
            with patch.object(app, 'responder_autoatendimento', return_value=('Orientação fictícia', source)):
                self.client.post('/servidor/chat', data={'pergunta': 'Pergunta fictícia ' + source})
            self.client.post('/servidor/resolvido')
        with app.db() as c:
            self.assertEqual(app.contar_auto_resolvidos(c), 3)
            self.assertEqual(app.contar_auto_resolvidos(c, somente_ia=True), 2)
            self.assertEqual(app.contar_auto_resolvidos(c, '2099-01-01', somente_ia=True), 0)
            self.assertEqual(app.indicadores(c)['auto_ia_resolvidos'], 2)
            self.assertEqual(app.metricas(c, 90)['auto_ia_resolvidos'], 2)

    def test_chat_restores_history_after_instance_storage_is_lost(self):
        first = self.client.post('/servidor/chat', data={'pergunta': 'A impressora fictícia não imprime'})
        state = re.search(r'id="estado-chat" name="estado_chat" value="([^"]+)"', first.text)[1]
        with app.db() as c:
            c.execute('DELETE FROM auto_msgs')
            c.execute('DELETE FROM autoatendimento')
        second = self.client.post('/servidor/chat', data={'pergunta': 'Já reiniciei, mas continua', 'estado_chat': state})
        self.assertIn('A impressora fictícia não imprime', second.text)
        self.assertIn('Já reiniciei, mas continua', second.text)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM auto_msgs').fetchone()[0], 4)
            c.execute('DELETE FROM auto_msgs')
            c.execute('DELETE FROM autoatendimento')
        refreshed = self.client.get('/servidor')
        self.assertIn('A impressora fictícia não imprime', refreshed.text)
        self.assertIn('Já reiniciei, mas continua', refreshed.text)
        self.client.post('/servidor/nova')
        self.assertNotIn('Já reiniciei, mas continua', self.client.get('/servidor').text)

    def test_chat_rejects_modified_or_unrelated_history(self):
        msgs = [{'autor': 'assistente', 'texto': 'Resposta falsa', 'fonte': 'llm', 'criado_em': app.agora()}]
        state = app.estado_conversa('token-ficticio', msgs)
        with app.db() as c:
            self.assertIsNone(app.restaurar_conversa(c, state[:-1] + ('0' if state[-1] != '0' else '1'), None))
            self.assertIsNone(app.restaurar_conversa(c, state, 'outro-token'))
            self.assertEqual(c.execute('SELECT count(*) FROM auto_msgs').fetchone()[0], 0)

    def test_demo_entry_identity_prefill_and_signout(self):
        login = self.client.get('/login')
        self.assertIn('Tipo de usuário', login.text)
        self.assertIn('Solicitante · Ana Souza', login.text)
        self.assertIn('Ana Souza', login.text)
        entry = self.client.post('/login/solicitante', data={'email': 'outro@example.com'})
        self.assertEqual(entry.status_code, 303)
        self.assertEqual(entry.headers['location'], '/servidor')
        home = self.client.get('/servidor')
        self.assertIn('Ana Souza', home.text)
        self.assertIn('Solicitante fictício', home.text)
        form = self.client.get('/servidor/solicitar')
        self.assertIn('value="Ana Souza"', form.text)
        self.assertIn('value="ana.souza.demo@example.com"', form.text)
        mine = self.client.get('/meus-chamados')
        self.assertEqual(mine.status_code, 303)
        self.assertIn('ana.souza.demo@example.com', self.client.get(mine.headers['location']).text)
        with app.db() as c:
            rows = c.execute('SELECT email FROM acessos_solicitante').fetchall()
        self.assertEqual([r['email'] for r in rows], [app.SOLICITANTE_DEMO['email']])
        self.assertEqual(self.client.get('/atendente').status_code, 303)
        self.client.get('/meus-chamados-sair')
        self.assertNotIn('value="Ana Souza"', self.client.get('/servidor/solicitar').text)

    def test_profile_dropdown_uses_existing_sessions_and_destinations(self):
        for uid, destination, cookie in ((0, '/servidor', 'acesso_solicitante'),
                                         (1, '/atendente', 'sess_painel'),
                                         (2, '/gestor', 'sess_painel'),
                                         (3, '/campo', 'sess_campo')):
            self.client.cookies.clear()
            r = self.client.post('/login', data={'uid': uid})
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers['location'], destination)
            self.assertIn(cookie, r.headers.get('set-cookie', ''))
        self.assertEqual(self.client.post('/login', data={'uid': -1}).status_code, 404)

    def test_expired_access_does_not_identify_or_prefill(self):
        self.client.post('/login/solicitante')
        expired = (datetime.now() - timedelta(days=app.ACESSO_DIAS + 1)).strftime('%Y-%m-%d %H:%M:%S')
        with app.db() as c:
            c.execute('UPDATE acessos_solicitante SET criado_em=?', (expired,))
        self.assertNotIn('value="Ana Souza"', self.client.get('/servidor/solicitar').text)
