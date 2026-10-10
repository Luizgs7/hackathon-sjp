"""Missões SJP — protótipo da plataforma genérica de tarefas gamificadas (Hackathon SJP / SIMOT).

Um único processo FastAPI + SQLite. Papéis: atendente, gestor, executor (web mobile em /campo)
e demandante (link com token em /validar/{token}). Integração bidirecional com o legado simulado
(legado.py) via API de entrada + outbox de saída. IA via endpoint compatível com OpenAI, com
fallback por regras.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import threading
import unicodedata
import zlib
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from jev import JevError, classify_task, route_conversation
from haiku import generate as generate_haiku
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rastro import (BASE as BASE_RASTRO, CRITERIOS, caminho_rua, calcular_trechos, destino as rastro_destino, distancia_km,
                    estado as rastro_estado, km_entre, ordenar_rota)
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

DB_PATH = Path(os.getenv("PLATAFORMA_DB", BASE / "plataforma.db"))
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
LEGADO_URL = os.getenv("LEGADO_URL", "http://localhost:8001")
API_KEY_LEGADO = os.getenv("API_KEY_LEGADO", "chave-demo-legado")
SECRET = os.getenv("SESSION_SECRET", "troque-este-segredo-em-producao")
LLM_MODEL = os.getenv("HAIKU_MODEL", "claude-haiku-5-5")
VAPID_PEM = BASE / "vapid_private.pem"
VAPID_SUB = os.getenv("VAPID_SUB", "mailto:simot@exemplo.sjp.pr.gov.br")
SEED_HISTORICO = os.getenv("SEED_HISTORICO", "true").lower() == "true"

log = logging.getLogger("missoes")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------- domínio

# Status oficiais do chamado: Novo, Encaminhado, Devolvido, Executado, Concluído, Cancelado e Reaberto.
# Enquanto está Encaminhado, o técnico registra etapas internas de campo (a caminho, em execução, impedido),
# que preservam o app do executor, a pausa do SLA nos impedimentos externos e a pontuação.
STATUS = {  # chave: (ícone Lucide em static/icones.svg, rótulo)
    "novo": ("inbox", "Novo"),
    "encaminhado": ("send", "Encaminhado"),
    "a_caminho": ("car", "Encaminhado · a caminho"),
    "em_execucao": ("wrench", "Encaminhado · em execução"),
    "impedido": ("octagon-alert", "Encaminhado · impedido"),
    "devolvido": ("undo-2", "Devolvido"),
    "executado": ("circle-check", "Executado – aguardando confirmação"),
    "concluido": ("badge-check", "Concluído"),
    "reaberto": ("rotate-ccw", "Reaberto"),
    "cancelado": ("circle-x", "Cancelado"),
}
# Etapa interna → status oficial (quadro, filtros, métricas e sincronização com o sistema de origem).
STATUS_OFICIAL = {k: k for k in STATUS} | {"a_caminho": "encaminhado", "em_execucao": "encaminhado", "impedido": "encaminhado"}
STATUS_OFICIAIS = ["novo", "encaminhado", "devolvido", "reaberto", "executado", "concluido", "cancelado"]
TRANSICOES = {
    "novo": {"encaminhado", "executado", "cancelado"},
    "devolvido": {"encaminhado", "executado", "cancelado"},
    "reaberto": {"encaminhado", "executado", "cancelado"},
    "encaminhado": {"encaminhado", "a_caminho", "devolvido", "cancelado"},
    "a_caminho": {"em_execucao", "devolvido", "cancelado"},
    "em_execucao": {"impedido", "executado", "devolvido", "cancelado"},
    "impedido": {"em_execucao", "cancelado"},
    "executado": {"concluido", "reaberto"},
    "concluido": set(),
    "cancelado": set(),
}
ATIVOS = ("encaminhado", "a_caminho", "em_execucao", "impedido")
FINAIS = ("concluido", "cancelado")
A_ENCAMINHAR = ("novo", "devolvido", "reaberto")
FILA_ATENDIMENTO = ("novo", "reaberto")  # devolvidos pelo técnico vão ao gestor técnico da área
MOTIVOS_DEVOLUCAO = [
    "Faltam informações do solicitante",
    "Chamado pertence a outra área",
    "Não é um problema técnico",
    "Outro",
]
PRIORIDADES = {"P1": "Crítica", "P2": "Alta", "P3": "Média", "P4": "Baixa"}
MOTIVOS_IMPEDIMENTO = [
    "Falta de material",
    "Falta de equipamento/ferramenta",
    "Depende de outra equipe",
    "Depende de terceiros/fornecedor",
    "Acesso ao local indisponível",
    "Outro",
]
# Motivos fora do controle do executor: pausam o relógio do SLA e não penalizam.
MOTIVOS_EXTERNOS = set(MOTIVOS_IMPEDIMENTO) - {"Outro"}

# Áreas de TI que recebem os chamados (tabela setores). A ordem define os ids dos dados de demonstração.
AREAS = ["Help Desk", "Suporte Técnico", "Telecom", "Datacenter", "Telefonia", "Fábrica de Software"]
AREA_NIVEL1 = 1  # Help Desk: tipos que o atendimento costuma resolver no 1º nível, sem acionar o time técnico

# Secretarias municipais de quem abre o chamado (base da análise de capacitação por secretaria).
SECRETARIAS = [
    "Administração", "Agricultura", "Assistência Social", "Comunicação", "Cultura", "Educação",
    "Esporte e Lazer", "Finanças", "Governo", "Meio Ambiente", "Obras", "Procuradoria-Geral", "Saúde",
    "Segurança Pública", "Transporte e Trânsito", "Urbanismo",
]

# Origem → setor padrão (cada sistema integrado é configurado aqui; novo sistema = nova entrada).
SISTEMAS_ORIGEM = {"legado": {"nome": "SisChamados Legado", "setor_id": 1, "api_key": API_KEY_LEGADO}}

REGRAS_PADRAO = {
    "pontos_por_complexidade": 20,
    "multiplicador_nota": {"1": 0.5, "2": 0.75, "3": 1.0, "4": 1.2, "5": 1.5},
    "sla_minutos": {"P1": 60, "P2": 240, "P3": 1440, "P4": 4320},
    "bonus_sla": 20,
    "bonus_sem_reabertura": 15,
    "bonus_desbloqueio_gestor": 10,
    "bonus_apoio_colega": 15,
}

BASE_CONHECIMENTO = {
    "Rede": "PR-TI-07 Ponto de rede sem conexão: 1) Conferir cabo do computador e LED da placa de rede. "
    "2) Testar com outro cabo patch. 3) Usar testador de cabos no ponto; se falhar, inspecionar o keystone/tomada RJ45 "
    "(material: keystone Cat6, alicate de inserção) e substituir. 4) No rack, localizar o patch panel do ponto e conferir a "
    "porta do switch (LED). 5) Testar outra porta do switch; porta defeituosa → acionar Infra de Redes. "
    "6) Registrar número do ponto e o que foi trocado.",
    "Wi-fi": "PR-TI-09 Wi-fi instável: 1) Verificar se o access point está energizado (PoE). 2) Checar interferência e "
    "quantidade de usuários. 3) Reiniciar o AP pelo controlador. 4) Se persistir, solicitar site survey à Infra de Redes.",
    "Impressora": "PR-TI-03 Impressora: 1) Conferir toner/papel e atolamento. 2) Imprimir página de configuração e "
    "conferir IP. 3) Reinstalar fila no servidor de impressão. 4) Sem toner → solicitar ao Almoxarifado TI.",
    "Computador": "PR-TI-01 Computador/sistema: 1) Verificar energia e cabos. 2) Reiniciar e checar mensagens de erro. "
    "3) Senha bloqueada → desbloqueio pelo AD com identificação do servidor. 4) Hardware com defeito → troca por "
    "equipamento reserva do Almoxarifado TI.",
    "Senha": "PR-TI-02 Senha e acesso: 1) Confirmar a identidade do servidor (matrícula e chefia). 2) Verificar no AD se a "
    "conta está bloqueada ou expirada. 3) Desbloquear/redefinir com senha temporária e troca obrigatória no próximo login. "
    "4) Acesso a pasta ou sistema → conferir grupo de permissão com a chefia antes de liberar.",
    "Sistema": "PR-TI-11 Erro ou dúvida em sistema municipal: 1) Registrar a tela, a mensagem de erro e o horário. "
    "2) Reproduzir com outro usuário/computador. 3) Dúvida de uso → orientar pelo manual do sistema. 4) Erro "
    "reproduzível → abrir ocorrência para a Fábrica de Software com os passos e prints.",
    "Datacenter": "PR-DC-01 Sistema ou servidor indisponível: 1) Confirmar se afeta todos os usuários (monitoramento). "
    "2) Verificar status da VM/serviço no hypervisor e espaço em disco. 3) Reiniciar o serviço conforme runbook. "
    "4) Persistindo, acionar plantão do Datacenter e comunicar as secretarias afetadas.",
    "Backup": "PR-DC-04 Restauração de arquivos: 1) Identificar caminho, nome do arquivo e data da última versão boa. "
    "2) Conferir permissão do solicitante na pasta. 3) Restaurar do snapshot/backup mais recente em pasta separada. "
    "4) Validar com o solicitante antes de sobrescrever.",
    "Telefonia": "PR-TF-03 Ramal ou linha: 1) Conferir cabo e alimentação do aparelho (PoE). 2) Testar o aparelho em outro "
    "ponto. 3) Verificar o ramal no PABX/IP (registro e categoria de ligação). 4) Celular corporativo → checar status "
    "do chip junto à operadora.",
}

# Autoatendimento}

# Autoatendimento: orientações seguras para servidores leigos (antes de abrir uma solicitação).
BASE_AUTOATENDIMENTO = {
    "Rede": "Sem internet/rede cabeada: confira se o cabo de rede (geralmente azul) está bem encaixado atrás do "
    "computador e na tomada da parede; reinicie o computador; pergunte se colegas próximos também estão sem acesso "
    "(se todos estiverem, informe isso). Anote o número/etiqueta da tomada de rede.",
    "Wi-fi": "Wi-fi: desligue e ligue o Wi-fi do aparelho; esqueça a rede e conecte novamente; aproxime-se do roteador.",
    "Impressora": "Impressora: confira se está ligada e com papel; abra a tampa e veja se há papel atolado (retire com "
    "cuidado); desligue por 30 segundos e ligue; veja se o painel mostra aviso de toner.",
    "Computador": "Computador/sistema: reinicie o computador; confira cabos de energia e do monitor; na senha, "
    "verifique o Caps Lock. Senha bloqueada precisa de atendimento com identificação.",
    "Senha": "Senha: confira o Caps Lock e o teclado numérico; aguarde 15 minutos se a conta acabou de bloquear por "
    "tentativas. Nunca informe sua senha a ninguém, nem ao suporte. Desbloqueio precisa de atendimento com identificação.",
    "Sistema": "Sistema: anote a mensagem de erro (ou tire uma foto da tela), feche e abra o sistema de novo e teste em "
    "outro navegador. Para dúvidas de uso, consulte o manual do sistema na intranet.",
    "Datacenter": "Sistema fora do ar: pergunte a colegas se também estão sem acesso. Se for geral, informe o nome do "
    "sistema e o horário; a equipe do Datacenter já pode estar atuando.",
    "Backup": "Arquivo apagado em pasta de rede: não salve novos arquivos com o mesmo nome; anote o caminho da pasta, o "
    "nome do arquivo e quando ele foi visto pela última vez.",
    "Telefonia": "Telefone/ramal: confira se o cabo do aparelho está bem encaixado e se a tela acende; tire da tomada e "
    "ligue de novo. Informe o número do ramal.",
}

# ------}

# ---------------------------------------------------------------- banco

SCHEMA = """
CREATE TABLE IF NOT EXISTS setores(id INTEGER PRIMARY KEY, nome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equipes(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, setor_id INTEGER REFERENCES setores(id));
CREATE TABLE IF NOT EXISTS tipos(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, setor_id INTEGER REFERENCES setores(id),
  complexidade INTEGER NOT NULL DEFAULT 2, palavras TEXT NOT NULL DEFAULT '', base_conhecimento TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, papeis TEXT NOT NULL,
  equipe_id INTEGER REFERENCES equipes(id), competencias TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS tarefas(
  id INTEGER PRIMARY KEY, origem TEXT NOT NULL DEFAULT 'web', external_id TEXT,
  titulo TEXT NOT NULL, descricao TEXT NOT NULL, local TEXT NOT NULL DEFAULT '', solicitante TEXT NOT NULL DEFAULT '',
  contato TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', secretaria TEXT NOT NULL DEFAULT '', setor_id INTEGER REFERENCES setores(id), tipo_id INTEGER REFERENCES tipos(id),
  prioridade TEXT, prioridade_ajustada_por TEXT, resolvido_atendimento_por TEXT, status TEXT NOT NULL DEFAULT 'novo', executor_id INTEGER REFERENCES usuarios(id),
  criado_por INTEGER REFERENCES usuarios(id), token TEXT NOT NULL UNIQUE,
  ia_status TEXT NOT NULL DEFAULT 'analisando', ia_fonte TEXT, ia_json TEXT,
  relato TEXT, resumo_ia TEXT, evidencia TEXT, nota INTEGER, comentario_demandante TEXT,
  reaberturas INTEGER NOT NULL DEFAULT 0, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL,
  UNIQUE(origem, external_id));
CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, tarefa_id INTEGER NOT NULL REFERENCES tarefas(id),
  usuario TEXT NOT NULL, papel TEXT NOT NULL, de TEXT, para TEXT, texto TEXT NOT NULL DEFAULT '', anexo TEXT,
  criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS impedimentos(id INTEGER PRIMARY KEY, tarefa_id INTEGER NOT NULL REFERENCES tarefas(id),
  motivo TEXT NOT NULL, detalhe TEXT NOT NULL DEFAULT '', externo INTEGER NOT NULL, aberto INTEGER NOT NULL DEFAULT 1,
  sugestao_ia TEXT, providencia TEXT, apoio_id INTEGER REFERENCES usuarios(id), resolvido_por TEXT,
  criado_em TEXT NOT NULL, resolvido_em TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, tarefa_id INTEGER NOT NULL, origem TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pendente', tentativas INTEGER NOT NULL DEFAULT 0, erro TEXT,
  criado_em TEXT NOT NULL, entregue_em TEXT);
CREATE TABLE IF NOT EXISTS pontos(id INTEGER PRIMARY KEY, usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
  equipe_id INTEGER, tarefa_id INTEGER, temporada INTEGER NOT NULL, pontos INTEGER NOT NULL,
  tipo TEXT NOT NULL, descricao TEXT NOT NULL, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS temporadas(numero INTEGER PRIMARY KEY, nome TEXT NOT NULL, inicio TEXT NOT NULL, fim TEXT);
CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_subs(id INTEGER PRIMARY KEY, usuario_id INTEGER NOT NULL, endpoint TEXT UNIQUE, sub_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY, tarefa_id INTEGER NOT NULL, autor TEXT NOT NULL,
  texto TEXT NOT NULL, fonte TEXT, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS autoatendimento(token TEXT PRIMARY KEY, resolvido INTEGER NOT NULL DEFAULT 0,
  solicitacao_id INTEGER, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auto_msgs(id INTEGER PRIMARY KEY, token TEXT NOT NULL REFERENCES autoatendimento(token),
  autor TEXT NOT NULL, texto TEXT NOT NULL, fonte TEXT, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversa_acessos(token TEXT PRIMARY KEY REFERENCES autoatendimento(token) ON DELETE CASCADE, email TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solicitacoes(id INTEGER PRIMARY KEY, protocolo TEXT UNIQUE, token TEXT NOT NULL UNIQUE,
  nome TEXT NOT NULL, local TEXT NOT NULL, contato TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
  secretaria TEXT NOT NULL DEFAULT '', titulo TEXT NOT NULL, descricao TEXT NOT NULL,
  tentativas TEXT NOT NULL DEFAULT '', setor_id INTEGER REFERENCES setores(id), via_ia INTEGER NOT NULL DEFAULT 0,
  conversa TEXT, status TEXT NOT NULL DEFAULT 'aguardando', motivo_descarte TEXT, tarefa_id INTEGER REFERENCES tarefas(id),
  registrada_por TEXT, criado_em TEXT NOT NULL, registrada_em TEXT);
CREATE TABLE IF NOT EXISTS acessos_solicitante(token TEXT PRIMARY KEY, email TEXT NOT NULL, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rota_ordem(executor_id INTEGER NOT NULL, tarefa_id INTEGER NOT NULL, pos INTEGER NOT NULL, PRIMARY KEY(executor_id, tarefa_id));
CREATE TABLE IF NOT EXISTS rota_prefs(executor_id INTEGER PRIMARY KEY, criterio TEXT NOT NULL DEFAULT 'misto');
CREATE TABLE IF NOT EXISTS rota_log(id INTEGER PRIMARY KEY, executor_id INTEGER NOT NULL, texto TEXT NOT NULL, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notificacoes(id INTEGER PRIMARY KEY, dest TEXT NOT NULL, titulo TEXT NOT NULL, texto TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', lida INTEGER NOT NULL DEFAULT 0, criada_em TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notificacoes_dest ON notificacoes(dest, lida, id);
"""


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def db():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def cfg(c, chave, padrao=None):
    r = c.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return json.loads(r["valor"]) if r else padrao


def set_cfg(c, chave, valor):
    c.execute("INSERT INTO config(chave,valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
              (chave, json.dumps(valor, ensure_ascii=False)))


# Regras que o gestor pode ligar ou desligar: desligada vale zero na pontuação (o valor configurado fica guardado).
REGRAS_ATIVAVEIS = ("pontos_por_complexidade", "bonus_sla", "bonus_sem_reabertura", "bonus_desbloqueio_gestor", "bonus_apoio_colega")


def regras_configuradas(c):
    return json.loads(json.dumps(cfg(c, "regras", REGRAS_PADRAO)))


def regras(c):
    r = regras_configuradas(c)
    ativas = cfg(c, "regras_ativas", {})
    for k in REGRAS_ATIVAVEIS:
        if ativas.get(k) is False:
            r[k] = 0
    return r


def temporada_atual(c):
    return c.execute("SELECT max(numero) n FROM temporadas").fetchone()["n"]


# Colunas incluídas depois da primeira versão: bancos já existentes recebem um ALTER TABLE na inicialização.
MIGRACOES = [
    ("autoatendimento", "jev_json", "TEXT"),
    ("usuarios", "disponivel", "INTEGER NOT NULL DEFAULT 1"),
    ("tarefas", "km_percorrido", "REAL NOT NULL DEFAULT 0"),
    ("usuarios", "area_id", "INTEGER"),
    ("tarefas", "prioridade_por", "TEXT"),
    ("autoatendimento", "modo", "TEXT NOT NULL DEFAULT 'ia'"),
    ("autoatendimento", "atendente_id", "INTEGER"),
    ("autoatendimento", "modo_desde", "TEXT"),
    ("tarefas", "email", "TEXT NOT NULL DEFAULT ''"),
    ("tarefas", "secretaria", "TEXT NOT NULL DEFAULT ''"),
    ("tarefas", "prioridade_ajustada_por", "TEXT"),
    ("tarefas", "resolvido_atendimento_por", "TEXT"),
    ("solicitacoes", "email", "TEXT NOT NULL DEFAULT ''"),
    ("solicitacoes", "secretaria", "TEXT NOT NULL DEFAULT ''"),
]


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for tabela, coluna, tipo in MIGRACOES:
            if coluna not in {r["name"] for r in c.execute(f"PRAGMA table_info({tabela})")}:
                c.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        if c.execute("SELECT count(*) n FROM usuarios").fetchone()["n"] == 0:
            seed(c)
        garantir_gestor_tecnico(c)
        if SEED_HISTORICO and not cfg(c, "historico_metricas"):
            seed_historico(c)
        organizar_demo(c)


def garantir_gestor_tecnico(c):
    """Perfil fictício adicional, também aplicado aos bancos existentes da demonstração."""
    # Cada gestor técnico responde por uma área e só enxerga os chamados dela.
    for nome, setor in (("Roberto Nunes", 2), ("Helena Prado", 3), ("Camila Duarte", 5)):
        c.execute("INSERT INTO usuarios(nome,papeis,competencias,avatar,area_id) SELECT ?,'gestor_tecnico','','',? "
                  "WHERE NOT EXISTS (SELECT 1 FROM usuarios WHERE nome=?)", (nome, setor, nome))
    c.execute("UPDATE usuarios SET area_id=2 WHERE papeis='gestor_tecnico' AND area_id IS NULL")


def organizar_demo(c):
    """Demonstração enxuta: 3 gestores técnicos (Suporte, Telecom e Telefonia), cada um com 1 técnico.
    Aplica-se também a bancos já existentes: o histórico dos técnicos removidos passa para os que ficam. Roda uma vez."""
    if cfg(c, "demo_3x1"):
        return
    ids = {r["nome"]: r["id"] for r in c.execute("SELECT id, nome FROM usuarios")}
    destino = {"Bruno Alves": "Marcos Vieira", "Juliana Rocha": "Rafael Costa", "Larissa Moura": "Diego Santos", "Tiago Ferreira": "Diego Santos"}
    for antigo, novo in destino.items():
        if antigo not in ids or novo not in ids:
            continue
        a, n = ids[antigo], ids[novo]
        eq = c.execute("SELECT equipe_id FROM usuarios WHERE id=?", (n,)).fetchone()["equipe_id"]
        c.execute("UPDATE tarefas SET executor_id=? WHERE executor_id=?", (n, a))
        c.execute("UPDATE tarefas SET criado_por=? WHERE criado_por=?", (n, a))
        c.execute("UPDATE pontos SET usuario_id=?, equipe_id=? WHERE usuario_id=?", (n, eq, a))
        c.execute("UPDATE impedimentos SET apoio_id=? WHERE apoio_id=?", (n, a))
        c.execute("UPDATE push_subs SET usuario_id=? WHERE usuario_id=?", (n, a))
        c.execute("DELETE FROM usuarios WHERE id=?", (a,))
    c.execute("DELETE FROM usuarios WHERE nome IN ('Otávio Brandão','Renato Paiva') AND papeis='gestor_tecnico'")
    # áreas e equipes sem técnico saem; os tipos delas passam para Suporte Técnico (Help Desk, Telecom, Telefonia e Suporte permanecem)
    for setor in (4, 6):
        c.execute("UPDATE tipos SET setor_id=2 WHERE setor_id=?", (setor,))
        c.execute("UPDATE tarefas SET setor_id=2 WHERE setor_id=?", (setor,))
        c.execute("UPDATE solicitacoes SET setor_id=2 WHERE setor_id=?", (setor,))
    for eq in (1, 4, 6):
        if not c.execute("SELECT 1 FROM usuarios WHERE equipe_id=?", (eq,)).fetchone():
            alvo = c.execute("SELECT id FROM equipes ORDER BY id LIMIT 1 OFFSET 1").fetchone()
            c.execute("UPDATE pontos SET equipe_id=NULL WHERE equipe_id=?", (eq,))
            c.execute("DELETE FROM equipes WHERE id=?", (eq,))
    for setor in (4, 6):
        if not c.execute("SELECT 1 FROM equipes WHERE setor_id=?", (setor,)).fetchone():
            c.execute("DELETE FROM setores WHERE id=?", (setor,))
    set_cfg(c, "demo_3x1", True)


def seed(c):
    c.executemany("INSERT INTO setores(id,nome) VALUES(?,?)", [(i, n) for i, n in enumerate(AREAS, 1)])
    c.executemany("INSERT INTO equipes(id,nome,setor_id) VALUES(?,?,?)", [
        (1, "Help Desk N1", 1), (2, "Suporte Técnico de Campo", 2), (3, "Telecom e Redes", 3),
        (4, "Datacenter e Servidores", 4), (5, "Telefonia", 5), (6, "Fábrica de Software", 6)])
    c.executemany("INSERT INTO tipos(id,nome,setor_id,complexidade,palavras,base_conhecimento) VALUES(?,?,?,?,?,?)", [
        (1, "Rede / conectividade", 3, 3, "rede,ponto de rede,internet,cabo,switch,sem conexão,link", "Rede"),
        (2, "Wi-fi", 3, 2, "wi-fi,wifi,sem fio,access point", "Wi-fi"),
        (3, "Computador / periféricos", 2, 2, "computador,pc,monitor,teclado,mouse,lento,não liga", "Computador"),
        (4, "Impressora", 2, 1, "impressora,toner,impressão,scanner", "Impressora"),
        (5, "Senha e acesso", 1, 1, "senha,bloqueada,bloqueado,acesso,login,e-mail,email,usuário", "Senha"),
        (6, "Dúvida de uso de sistema", 1, 1, "dúvida,duvida,como faço,como usar,orientação,não sei", "Sistema"),
        (7, "Servidor / sistema fora do ar", 4, 4, "fora do ar,servidor,indisponível,todos sem,caiu o sistema", "Datacenter"),
        (8, "Backup e pastas de rede", 4, 3, "backup,pasta de rede,arquivo apagado,restaurar,unidade de rede", "Backup"),
        (9, "Ramal / telefone fixo", 5, 2, "ramal,telefone,sem linha,ligação,ligacao,pabx", "Telefonia"),
        (10, "Celular corporativo", 5, 2, "celular,chip,linha móvel,smartphone", "Telefonia"),
        (11, "Erro em sistema municipal", 6, 3, "erro,bug,mensagem de erro,não salva,travou ao salvar", "Sistema"),
        (12, "Melhoria / nova funcionalidade", 6, 4, "melhoria,relatório,relatorio,nova funcionalidade,sugestão", ""),
    ])
    c.executemany("INSERT INTO usuarios(id,nome,papeis,equipe_id,competencias,avatar) VALUES(?,?,?,?,?,?)", [
        (1, "Carlos Lima", "atendente", None, "", ""),
        (2, "Paula Mendes", "gestor,atendente", None, "", ""),
        (3, "Rafael Costa", "executor", 3, "redes, cabeamento, keystone, switch, wi-fi", ""),
        (4, "Diego Santos", "executor", 2, "computadores, impressoras, periféricos", ""),
        (5, "Bruno Alves", "executor", 1, "senhas, acessos, e-mail, orientação de sistemas", ""),
        (6, "Juliana Rocha", "executor", 4, "servidores, backup, virtualização, storage", ""),
        (7, "Marcos Vieira", "executor", 5, "ramais, pabx, telefonia, celulares", ""),
        (8, "Larissa Moura", "executor", 6, "sistemas municipais, banco de dados, correções, relatórios", ""),
        (9, "Tiago Ferreira", "executor", 2, "computadores, impressoras, wi-fi", ""),
    ])
    set_cfg(c, "regras", REGRAS_PADRAO)
    c.execute("INSERT INTO temporadas(numero,nome,inicio,fim) VALUES(1,'Temporada de Setembro','2026-09-01 00:00:00','2026-09-30 23:59:59')")
    c.execute("INSERT INTO temporadas(numero,nome,inicio) VALUES(2,'Temporada de Outubro','2026-10-01 00:00:00')")
    # histórico fictício de pontuação (temporada encerrada e atual) para o ranking não começar vazio
    hist = [(4, 2, 1, 210), (5, 1, 1, 160), (6, 4, 1, 190), (3, 3, 1, 120), (7, 5, 1, 90), (8, 6, 1, 140), (9, 2, 1, 100),
            (4, 2, 2, 140), (5, 1, 2, 90), (6, 4, 2, 110), (3, 3, 2, 60), (7, 5, 2, 50), (8, 6, 2, 80), (9, 2, 2, 70),
            (2, None, 2, 20)]
    for uid, eq, temp, pts in hist:
        c.execute("INSERT INTO pontos(usuario_id,equipe_id,tarefa_id,temporada,pontos,tipo,descricao,criado_em) "
                  "VALUES(?,?,NULL,?,?,'definitivo','Histórico de missões (dados fictícios)',?)",
                  (uid, eq, temp, pts, agora()))
    # algumas tarefas em andamento para o painel não começar vazio
    exemplos = [
        ("Wi-fi instável na biblioteca", "Wi-fi cai a cada 10 minutos na biblioteca da Escola Municipal Afonso Pena.",
         "Escola Municipal Afonso Pena – Biblioteca", "Marcos Pereira", "Educação", 3, 2, "P3", "em_execucao", 3),
        ("Computador não liga – recepção", "Computador da recepção não liga desde ontem.",
         "CRAS Centro – Recepção", "Lúcia Andrade", "Assistência Social", 2, 3, "P3", "encaminhado", 4),
        ("Ramal sem linha no protocolo", "O ramal 2231 do protocolo está sem linha desde a manhã.",
         "Paço Municipal – Protocolo", "Roberto Dias", "Administração", 5, 9, "P2", "a_caminho", 7),
    ]
    for tit, desc, loc, sol, sec, setor, tipo, prio, st, ex in exemplos:
        ts = agora()
        cur = c.execute(
            "INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,email,secretaria,setor_id,tipo_id,prioridade,status,"
            "executor_id,criado_por,token,ia_status,ia_fonte,criado_em,atualizado_em) "
            "VALUES('web',?,?,?,?,?,?,?,?,?,?,?,2,?,'aceita','regras',?,?)",
            (tit, desc, loc, sol, email_ficticio(sol), sec, setor, tipo, prio, st, ex, secrets.token_urlsafe(16), ts, ts))
        registrar_evento(c, cur.lastrowid, "Paula Mendes", "gestor", "Tarefa cadastrada e atribuída (dados de exemplo)",
                         None, "encaminhado")
        if st != "encaminhado":
            registrar_evento(c, cur.lastrowid, "Sistema", "seed", "Andamento de exemplo", "encaminhado", st)


def email_ficticio(nome):
    base = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower().split()
    return f"{base[0]}.{base[-1]}@sjp.pr.gov.br" if base else ""


# Histórico fictício de chamados resolvidos (12 semanas) para o painel de métricas não começar vazio. Cada secretaria
# tem um perfil de demanda, para que a análise de capacitação encontre padrões de recorrência.
PERFIS_SECRETARIA = {
    "Educação": (30, {2: 5, 4: 4, 5: 3, 3: 2, 6: 2, 1: 1}),
    "Saúde": (26, {5: 5, 11: 3, 3: 3, 7: 2, 9: 1, 1: 1}),
    "Administração": (18, {4: 5, 5: 3, 9: 2, 6: 2, 3: 1}),
    "Assistência Social": (12, {3: 3, 5: 3, 6: 2, 4: 2, 10: 1}),
    "Finanças": (8, {11: 4, 6: 3, 12: 2, 8: 1}),
    "Obras": (7, {10: 3, 9: 2, 3: 1, 8: 1}),
    "Urbanismo": (5, {12: 2, 11: 2, 8: 1, 4: 1}),
    "Cultura": (4, {2: 2, 9: 1, 3: 1}),
}
EXEMPLOS_TIPO = {
    1: ["Sem internet no computador da sala", "Ponto de rede não funciona", "Rede caiu no setor"],
    2: ["Wi-fi não conecta nos notebooks", "Wi-fi caindo toda hora", "Celular não encontra a rede Wi-fi"],
    3: ["Computador muito lento", "Monitor não liga", "Teclado parou de funcionar", "Computador não liga"],
    4: ["Impressora com papel atolado", "Impressora não imprime", "Aviso de toner baixo", "Impressora sumiu da lista"],
    5: ["Senha bloqueada no sistema", "Esqueci a senha do e-mail", "Sem acesso à pasta do setor", "Usuário expirado"],
    6: ["Como emitir relatório no sistema", "Dúvida para anexar documento no protocolo", "Como assinar documento digitalmente"],
    7: ["Sistema de prontuário fora do ar", "Servidor de arquivos indisponível", "Sistema de protocolo não abre para ninguém"],
    8: ["Arquivo apagado da pasta de rede", "Restaurar backup de planilha", "Unidade de rede não aparece"],
    9: ["Ramal sem linha", "Telefone não faz ligação externa", "Ramal com chiado"],
    10: ["Chip do celular corporativo bloqueado", "Celular corporativo sem sinal", "Solicitar linha móvel"],
    11: ["Erro ao salvar cadastro no sistema", "Mensagem de erro ao emitir guia", "Sistema trava ao gerar nota"],
    12: ["Novo relatório de atendimentos", "Melhoria na tela de cadastro", "Incluir campo no formulário"],
}
UNIDADES = {
    "Educação": ["Escola Municipal Afonso Pena", "CMEI Jardim Ipê", "Escola Municipal Rui Barbosa"],
    "Saúde": ["UBS Vila Nova", "UBS Central", "UPA Afonso Pena"],
    "Administração": ["Paço Municipal – RH", "Paço Municipal – Protocolo"],
    "Assistência Social": ["CRAS Centro", "CREAS Borda do Campo"],
    "Finanças": ["Paço Municipal – Tributação"],
    "Obras": ["Pátio de Obras"],
    "Urbanismo": ["Paço Municipal – Urbanismo"],
    "Cultura": ["Teatro Municipal", "Biblioteca Pública"],
}
NOMES_FICTICIOS = ["Ana Souza", "Bruna Teixeira", "César Moura", "Denise Prado", "Eduardo Ramos", "Fernanda Luz",
                   "Gustavo Nunes", "Helena Castro", "Igor Batista", "Joana Martins", "Kátia Freitas", "Leonardo Reis"]


def seed_historico(c):
    rnd = random.Random(42)
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tipos = {t["id"]: t for t in c.execute("SELECT * FROM tipos")}
    execs = {}
    for e in carga_executores(c):
        execs.setdefault(e["setor_id"], []).append(e)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    for semana in range(12, 0, -1):
        crescimento = 1 + (12 - semana) * 0.03  # demanda crescendo levemente: dá tendência ao gráfico
        for sec, (por_semana_x10, pesos) in PERFIS_SECRETARIA.items():
            qtd = int(por_semana_x10 / 10 * crescimento + rnd.random())
            for _ in range(qtd):
                tipo_id = rnd.choices(list(pesos), weights=list(pesos.values()))[0]
                tp = tipos[tipo_id]
                criado = hoje - timedelta(days=semana * 7 - rnd.randint(0, 4), hours=-rnd.randint(8, 17),
                                          minutes=-rnd.randint(0, 59))
                if criado > datetime.now() - timedelta(days=2):
                    continue
                prio = rnd.choices(["P1", "P2", "P3", "P4"], weights=[1, 4, 8, 2])[0]
                prio_ia = prio if rnd.random() > 0.15 else rnd.choice([p for p in PRIORIDADES if p != prio])
                ex = rnd.choice(execs[tp["setor_id"]])
                sol = rnd.choice(NOMES_FICTICIOS)
                t_atr = criado + timedelta(minutes=rnd.randint(5, 90))
                t_cam = t_atr + timedelta(minutes=rnd.randint(5, 60))
                t_exe = t_cam + timedelta(minutes=rnd.randint(10, 60))
                t_con = t_exe + timedelta(minutes=rnd.randint(15, 60 * tp["complexidade"] * 2))
                t_res = t_con + timedelta(hours=rnd.randint(1, 30))
                reaberta = rnd.random() < 0.07
                nota = rnd.choices([5, 4, 3, 2], weights=[6, 4, 2, 1 if reaberta else 0.2])[0]
                ia = {"tipo_id": tipo_id, "prioridade": prio_ia, "categoria": tp["nome"], "complexidade": tp["complexidade"],
                      "justificativa": "Histórico fictício", "resumo": "", "informacoes_faltantes": [],
                      "executor_id": ex["id"], "motivo_executor": ""}
                cur = c.execute(
                    "INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,email,secretaria,setor_id,tipo_id,"
                    "prioridade,prioridade_ajustada_por,status,executor_id,criado_por,token,ia_status,ia_fonte,ia_json,"
                    "relato,nota,reaberturas,criado_em,atualizado_em) "
                    "VALUES('web',?,?,?,?,?,?,?,?,?,?,'concluido',?,1,?,?,'regras',?,?,?,?,?,?)",
                    (rnd.choice(EXEMPLOS_TIPO[tipo_id]), "Chamado do histórico fictício para demonstração de métricas.",
                     rnd.choice(UNIDADES[sec]), sol, email_ficticio(sol), sec, tp["setor_id"], tipo_id, prio,
                     "Carlos Lima" if prio != prio_ia else None, ex["id"], secrets.token_urlsafe(16),
                     "aceita" if prio == prio_ia else "editada", json.dumps(ia, ensure_ascii=False),
                     "Atendimento realizado (histórico fictício).", nota, int(reaberta), fmt(criado), fmt(t_res)))
                tid = cur.lastrowid
                passos = [(criado, "Atendimento", "atendente", None, "novo"), (t_atr, "Paula Mendes", "gestor", "novo", "encaminhado"),
                          (t_cam, ex["nome"], "executor", "encaminhado", "a_caminho"), (t_exe, ex["nome"], "executor", "a_caminho", "em_execucao"),
                          (t_con, ex["nome"], "executor", "em_execucao", "executado"), (t_res, sol, "demandante", "executado", "concluido")]
                for quando, quem, papel, de, para in passos:
                    c.execute("INSERT INTO eventos(tarefa_id,usuario,papel,de,para,texto,criado_em) VALUES(?,?,?,?,?,?,?)",
                              (tid, quem, papel, de, para, "Histórico fictício", fmt(quando)))
    set_cfg(c, "historico_metricas", True)


# ---------------------------------------------------------------- trilha, status e outbox

def registrar_evento(c, tarefa_id, usuario, papel, texto, de=None, para=None, anexo=None):
    c.execute("INSERT INTO eventos(tarefa_id,usuario,papel,de,para,texto,anexo,criado_em) VALUES(?,?,?,?,?,?,?,?)",
              (tarefa_id, usuario, papel, de, para, texto, anexo, agora()))


def enfileirar_outbox(c, t, status, texto, responsavel):
    """Transactional outbox: a atualização ao sistema de origem é gravada na mesma transação da mudança."""
    if t["origem"] == "web" or not t["external_id"]:
        return
    oficial = STATUS_OFICIAL[status]
    payload = {"external_id": t["external_id"], "tarefa_id": t["id"], "status": oficial,
               "status_label": STATUS[oficial][1], "etapa": STATUS[status][1] if status != oficial else None,
               "texto": texto, "responsavel": responsavel, "data": agora()}
    c.execute("INSERT INTO outbox(tarefa_id,origem,payload,criado_em) VALUES(?,?,?,?)",
              (t["id"], t["origem"], json.dumps(payload, ensure_ascii=False), agora()))


def mudar_status(c, t, novo, usuario, papel, texto=""):
    if novo not in TRANSICOES[t["status"]]:
        raise HTTPException(409, f"Transição inválida: {STATUS[t['status']][1]} → {STATUS[novo][1]}")
    c.execute("UPDATE tarefas SET status=?, atualizado_em=? WHERE id=?", (novo, agora(), t["id"]))
    registrar_evento(c, t["id"], usuario["nome"] if usuario else "Demandante", papel, texto, t["status"], novo)
    enfileirar_outbox(c, t, novo, texto, usuario["nome"] if usuario else t["solicitante"])
    _notificar_status(c, t, novo, usuario, texto)


_outbox_lock = threading.Lock()


def processar_outbox():
    """Entrega as atualizações pendentes em ordem; para na primeira falha (preserva a sequência)."""
    if not _outbox_lock.acquire(blocking=False):
        return
    try:
        with db() as c:
            pendentes = c.execute("SELECT * FROM outbox WHERE status='pendente' ORDER BY id").fetchall()
        for o in pendentes:
            payload = json.loads(o["payload"]) | {"sequencia": o["id"]}
            try:
                r = httpx.post(f"{LEGADO_URL}/api/atualizacoes", json=payload, timeout=5,
                               headers={"X-Api-Key": SISTEMAS_ORIGEM[o["origem"]]["api_key"]})
                r.raise_for_status()
                erro = None
            except Exception as e:  # noqa: BLE001 — qualquer falha de rede mantém a atualização na fila
                erro = str(e)[:200] or e.__class__.__name__
            with db() as c:
                if erro is None:
                    c.execute("UPDATE outbox SET status='entregue', entregue_em=?, tentativas=tentativas+1, erro=NULL "
                              "WHERE id=?", (agora(), o["id"]))
                else:
                    c.execute("UPDATE outbox SET tentativas=tentativas+1, erro=? WHERE id=?", (erro, o["id"]))
            if erro:
                break
    finally:
        _outbox_lock.release()


def em_segundo_plano(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ---------------------------------------------------------------- IA

def llm(messages, max_tokens=1200, temperature=0.2, effort=None):
    # temperature mantido na assinatura para os chamadores existentes; não enviado ao Haiku.
    return generate_haiku(messages, max_tokens, effort=effort)


def extrair_json(texto):
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError("sem JSON na resposta")
    return json.loads(m.group(0))


def carga_executores(c, setor_id=None, todos=False):
    sql = ("SELECT u.*, e.nome equipe, e.setor_id, (SELECT count(*) FROM tarefas t WHERE t.executor_id=u.id AND "
           f"t.status IN ({','.join('?' * len(ATIVOS))})) carga, "
           "(SELECT count(*) FROM tarefas t WHERE t.executor_id=u.id AND t.status='concluido') resolvidas "
           "FROM usuarios u JOIN equipes e ON e.id=u.equipe_id WHERE u.papeis LIKE '%executor%'")
    if not todos:
        sql += " AND u.disponivel=1"
    params = list(ATIVOS)
    if setor_id:
        sql += " AND e.setor_id=?"
        params.append(setor_id)
    return c.execute(sql + " ORDER BY carga, u.nome", params).fetchall()


def triagem_por_regras(c, t):
    texto = f"{t['titulo']} {t['descricao']} {t['local']}".lower()
    # todas as áreas entram na disputa (o chamado pode ter sido aberto na área errada); empate favorece a área escolhida
    tipos = c.execute("SELECT * FROM tipos ORDER BY setor_id<>?, id", (t["setor_id"] or 0,)).fetchall()
    melhor, acertos = None, 0
    for tp in tipos:
        n = sum(1 for p in tp["palavras"].split(",") if p.strip() and p.strip() in texto)
        if n > acertos:
            melhor, acertos = tp, n
    melhor = melhor or tipos[0]
    if any(p in texto for p in ("urgente", "parado", "todos sem", "pronto atendimento", "hospital", "incêndio", "choque")):
        prio, just = "P1", "Termos de urgência ou serviço essencial parado."
    elif any(p in texto for p in ("ubs", "saúde", "saude", "vacina", "escola", "cmei", "atendimento ao público")):
        prio, just = "P2", "Afeta unidade de atendimento direto à população (saúde/educação)."
    elif any(p in texto for p in ("quando possível", "melhoria", "sugestão")):
        prio, just = "P4", "Solicitação sem impacto imediato."
    else:
        prio, just = "P3", "Impacto localizado, sem indicação de urgência."
    faltantes = []
    if melhor["base_conhecimento"] == "Rede" and not re.search(r"\d", t["descricao"]):
        faltantes.append("Número/identificação do ponto de rede (etiqueta na tomada)")
    if not t["contato"]:
        faltantes.append("Telefone ou ramal de contato do solicitante")
    if len(t["local"]) < 8:
        faltantes.append("Local exato (sala/andar)")
    execs = carga_executores(c, melhor["setor_id"])
    palavra_tipo = melhor["base_conhecimento"].lower() or melhor["nome"].lower()
    compat = [e for e in execs if any(w in e["competencias"].lower() for w in palavra_tipo.split("/"))
              or palavra_tipo[:4] in e["competencias"].lower()]
    escolhido = (compat or execs)[0] if execs else None
    nivel1 = melhor["setor_id"] == AREA_NIVEL1 and prio not in ("P1",)
    return {
        "resolvivel_no_atendimento": nivel1,
        "orientacao_atendimento": BASE_AUTOATENDIMENTO.get(melhor["base_conhecimento"], "") if nivel1 else "",
        "tipo_id": melhor["id"], "prioridade": prio, "justificativa": just, "categoria": melhor["nome"],
        "complexidade": melhor["complexidade"], "resumo": t["titulo"], "informacoes_faltantes": faltantes,
        "executor_id": escolhido["id"] if escolhido else None,
        "motivo_executor": (f"Competência compatível e menor carga atual ({escolhido['carga']} missões ativas)."
                            if escolhido else "Nenhum executor disponível no setor."),
    }


def triar(tarefa_id):
    """JEV sugere classificação; regras calculam elegibilidade; humano decide."""
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE id=?", (tarefa_id,)).fetchone()
        if not t or t["ia_status"] != "analisando":
            return
        # Token exclusivo: uma retriagem invalida retornos anteriores, mesmo no mesmo segundo.
        request_id = secrets.token_hex(16)
        c.execute("UPDATE tarefas SET ia_json=? WHERE id=?",
                  (json.dumps({"request_id": request_id}), tarefa_id))
        tipos = c.execute("SELECT t.*, s.nome setor FROM tipos t JOIN setores s ON s.id=t.setor_id").fetchall()
        r = triagem_por_regras(c, t)
    fonte = "regras"
    try:
        decision = classify_task(t, tipos)
        tipo = decision["answers"]["tipo"]["choice"]
        prioridade = decision["answers"]["prioridade"]["choice"]
        if tipo != "revisar":
            selected = next(x for x in tipos if str(x["id"]) == tipo)
            # Reaproveitar regras de completude e competência para a categoria escolhida.
            with db() as c:
                candidates = carga_executores(c, selected["setor_id"])
            keyword = selected["base_conhecimento"].lower() or selected["nome"].lower()
            compatible = [e for e in candidates if any(w in e["competencias"].lower() for w in keyword.split("/"))
                          or keyword[:4] in e["competencias"].lower()]
            executor = (compatible or candidates)[0] if candidates else None
            missing = []
            if not t["contato"]:
                missing.append("Telefone ou ramal de contato do solicitante")
            if len(t["local"]) < 8:
                missing.append("Local exato (sala/andar)")
            if selected["base_conhecimento"] == "Rede" and not re.search(r"\d", t["descricao"]):
                missing.append("Número/identificação do ponto de rede")
            r.update(tipo_id=selected["id"], categoria=selected["nome"], complexidade=selected["complexidade"],
                     executor_id=executor["id"] if executor else None, informacoes_faltantes=missing,
                     motivo_executor=(f"Competência e carga atual: {executor['carga']} tarefas ativas."
                                      if executor else "Nenhum executor disponível no setor."))
        if prioridade != "revisar":
            r["prioridade"] = prioridade
        r["revisao_necessaria"] = tipo == "revisar" or prioridade == "revisar"
        r["resolvivel_no_atendimento"] = (tipo != "revisar" and selected["setor_id"] == AREA_NIVEL1
                                          and r["prioridade"] != "P1" and not r["revisao_necessaria"])
        r["orientacao_atendimento"] = (BASE_AUTOATENDIMENTO.get(selected["base_conhecimento"], "")
                                       if r["resolvivel_no_atendimento"] else "")
        r["justificativa"] = ("Informação insuficiente ou fora do catálogo; conferir os valores provisórios por regras."
                              if r["revisao_necessaria"] else
                              "Classificação sugerida pela IA a partir do relato e dos critérios de impacto; requer conferência.")
        r["jev"] = decision
        fonte = "jev"
    except JevError as exc:
        log.warning("triagem indisponível: %s; usando regras", exc)
    with db() as c:
        atual = c.execute("SELECT * FROM tarefas WHERE id=?", (tarefa_id,)).fetchone()
        if (not atual or atual["ia_status"] != "analisando"
                or json.loads(atual["ia_json"] or "{}").get("request_id") != request_id
                or any(atual[k] != t[k] for k in ("titulo", "descricao", "local", "secretaria", "setor_id", "status"))):
            return
        saved = c.execute("UPDATE tarefas SET ia_status='sugerida', ia_fonte=?, ia_json=?, atualizado_em=? "
                          "WHERE id=? AND ia_status='analisando' AND ia_json=? "
                          "AND titulo IS ? AND descricao IS ? AND local IS ? AND secretaria IS ? "
                          "AND setor_id IS ? AND status IS ?",
                          (fonte, json.dumps(r, ensure_ascii=False), agora(), tarefa_id, atual["ia_json"],
                           t["titulo"], t["descricao"], t["local"], t["secretaria"], t["setor_id"], t["status"]))
        if not saved.rowcount:
            return
        registrar_evento(c, tarefa_id, "Assistente IA", "ia",
                         f"Sugestão de triagem: {r.get('categoria')} · {r['prioridade']} – {r['justificativa']} "
                         f"(fonte: {'IA' if fonte == 'jev' else 'regras'}; aguardando avaliação do atendimento)")


def sugerir_apoio(impedimento_id):
    with db() as c:
        i = c.execute("SELECT i.*, t.titulo, t.descricao, t.local FROM impedimentos i JOIN tarefas t ON t.id=i.tarefa_id "
                      "WHERE i.id=?", (impedimento_id,)).fetchone()
        equipes = [dict(x) for x in c.execute("SELECT e.nome, s.nome setor FROM equipes e JOIN setores s ON s.id=e.setor_id")]
        recorrentes = c.execute("SELECT count(*) n FROM impedimentos WHERE motivo=? AND id<>?",
                                (i["motivo"], i["id"])).fetchone()["n"]
    try:
        sug = llm([{"role": "user", "content":
                    "Um técnico de campo da prefeitura reportou um impedimento. Em no máximo 2 frases, em português, "
                    "sugira ao gestor qual equipe acionar e qual providência tomar. Não invente nomes de pessoas.\n"
                    f"Equipes: {json.dumps(equipes, ensure_ascii=False)}\nTarefa: {i['titulo']} – {i['descricao']} "
                    f"({i['local']})\nImpedimento: {i['motivo']} – {i['detalhe']}"}], max_tokens=600)
    except Exception as e:  # noqa: BLE001
        log.warning("sugestão de apoio LLM falhou (%s)", e)
        mapa = {"Falta de material": "Acionar Suporte e Almoxarifado TI para separar e enviar o material ao local.",
                "Falta de equipamento/ferramenta": "Acionar Suporte e Almoxarifado TI para emprestar o equipamento.",
                "Depende de outra equipe": "Encaminhar apoio da equipe responsável e manter o executor informado.",
                "Depende de terceiros/fornecedor": "Abrir solicitação ao fornecedor e registrar o prazo informado.",
                "Acesso ao local indisponível": "Contatar a chefia da unidade para liberar o acesso."}
        sug = "[regras] " + mapa.get(i["motivo"], "Avaliar o impedimento com o executor.")
    if recorrentes:
        sug += f" Atenção, motivo recorrente: {recorrentes} ocorrência(s) anteriores de '{i['motivo']}'."
    with db() as c:
        c.execute("UPDATE impedimentos SET sugestao_ia=? WHERE id=?", (sug, impedimento_id))


def resumir_conclusao(tarefa_id):
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE id=?", (tarefa_id,)).fetchone()
    try:
        resumo = llm([{"role": "user", "content":
                       "Reescreva o relato técnico abaixo para a pessoa que pediu o serviço, em linguagem simples, "
                       "em até 2 frases, sem jargões. Responda apenas o texto.\n"
                       f"Pedido: {t['titulo']} – {t['descricao']}\nRelato do técnico: {t['relato']}"}], max_tokens=600)
    except Exception as e:  # noqa: BLE001
        log.warning("resumo LLM falhou (%s)", e)
        resumo = t["relato"]
    with db() as c:
        c.execute("UPDATE tarefas SET resumo_ia=? WHERE id=?", (resumo, tarefa_id))


def responder_chat(c, t, pergunta):
    tipo = c.execute("SELECT * FROM tipos WHERE id=?", (t["tipo_id"],)).fetchone() if t["tipo_id"] else None
    historico = c.execute("SELECT autor, texto FROM chat WHERE tarefa_id=? ORDER BY id DESC LIMIT 6", (t["id"],)).fetchall()
    similares = c.execute("SELECT titulo, relato FROM tarefas WHERE status='concluido' AND tipo_id=? AND relato IS NOT NULL "
                          "AND id<>? ORDER BY id DESC LIMIT 3", (t["tipo_id"], t["id"])).fetchall()
    msgs = [{"role": "system", "content":
             "Você é o Copiloto de Campo da Prefeitura de São José dos Pinhais. Ajude o técnico a diagnosticar e "
             "resolver a tarefa com passos curtos e numerados (máx. 6), cite o procedimento interno usado (ex.: PR-TI-07) "
             "e indique materiais prováveis. Se houver risco de segurança, avise primeiro. Responda em português, "
             "em texto simples, sem markdown (sem asteriscos, cerquilhas ou tabelas).\n"
             f"BASE DE CONHECIMENTO: {json.dumps(BASE_CONHECIMENTO, ensure_ascii=False)}\n"
             f"TAREFA: {t['titulo']} – {t['descricao']} – local: {t['local']} – tipo: {tipo['nome'] if tipo else 'não definido'}\n"
             f"CASOS RESOLVIDOS SIMILARES: {json.dumps([dict(s) for s in similares], ensure_ascii=False)}"}]
    for h in reversed(historico):
        msgs.append({"role": "user" if h["autor"] == "executor" else "assistant", "content": h["texto"]})
    msgs.append({"role": "user", "content": pergunta})
    try:
        resposta = llm(msgs, max_tokens=1500, effort=os.getenv("HAIKU_CHAT_EFFORT", "low"))
        return re.sub(r"\*\*|^#+\s*", "", resposta, flags=re.M), "llm"
    except Exception as e:  # noqa: BLE001
        log.warning("chat LLM falhou (%s)", e)
        chave = tipo["base_conhecimento"] if tipo and tipo["base_conhecimento"] else None
        if not chave:
            texto = (pergunta + " " + t["descricao"]).lower()
            chave = next((k for k in BASE_CONHECIMENTO if k.lower() in texto), None)
        if chave:
            return f"(IA indisponível – procedimento da base de conhecimento)\n{BASE_CONHECIMENTO[chave]}", "regras"
        return "(IA indisponível) Não encontrei procedimento para este caso. Acione o gestor pelo impedimento.", "regras"


# ---------------------------------------------------------------- gamificação

def lancar(c, usuario_id, tarefa_id, pontos, tipo, descricao):
    u = c.execute("SELECT equipe_id FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    c.execute("INSERT INTO pontos(usuario_id,equipe_id,tarefa_id,temporada,pontos,tipo,descricao,criado_em) "
              "VALUES(?,?,?,?,?,?,?,?)", (usuario_id, u["equipe_id"], tarefa_id, temporada_atual(c), int(round(pontos)),
                                          tipo, descricao, agora()))


def complexidade(c, t):
    ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
    if t["ia_status"] in ("aceita", "editada") and ia.get("complexidade"):
        return int(ia["complexidade"])
    tp = c.execute("SELECT complexidade FROM tipos WHERE id=?", (t["tipo_id"],)).fetchone()
    return tp["complexidade"] if tp else 2


def minutos(a, b):
    fa, fb = (datetime.strptime(x, "%Y-%m-%d %H:%M:%S") for x in (a, b))
    return (fb - fa).total_seconds() / 60


def tempo_liquido(c, t):
    """Minutos entre a atribuição e a conclusão, descontando impedimentos externos (fora do controle do executor)."""
    ini = c.execute("SELECT max(criado_em) m FROM eventos WHERE tarefa_id=? AND para='encaminhado'", (t["id"],)).fetchone()["m"]
    fim = c.execute("SELECT max(criado_em) m FROM eventos WHERE tarefa_id=? AND para='executado'", (t["id"],)).fetchone()["m"]
    if not ini or not fim:
        return None
    pausas = sum(minutos(i["criado_em"], i["resolvido_em"]) for i in c.execute(
        "SELECT * FROM impedimentos WHERE tarefa_id=? AND externo=1 AND resolvido_em IS NOT NULL", (t["id"],)))
    return max(0.0, minutos(ini, fim) - pausas)


def pontuar_conclusao(c, t):
    rg = regras(c)
    base = rg["pontos_por_complexidade"] * complexidade(c, t)
    lancar(c, t["executor_id"], t["id"], base, "provisorio",
           f"Missão #{t['id']} concluída (complexidade {complexidade(c, t)}) – aguardando confirmação")
    return base


def pontuar_validacao(c, t, nota):
    rg = regras(c)
    c.execute("UPDATE pontos SET tipo='convertido' WHERE tarefa_id=? AND tipo IN ('provisorio','suspenso')", (t["id"],))
    cx = complexidade(c, t)
    mult = float(rg["multiplicador_nota"].get(str(nota), 1.0))
    total = rg["pontos_por_complexidade"] * cx * mult
    lancar(c, t["executor_id"], t["id"], total, "definitivo",
           f"Missão #{t['id']} confirmada: {rg['pontos_por_complexidade']}×{cx} (complexidade) × {mult} (nota {nota}★)")
    liq = tempo_liquido(c, t)
    sla = rg["sla_minutos"].get(t["prioridade"] or "P3", 1440)
    if liq is not None and liq <= sla:
        lancar(c, t["executor_id"], t["id"], rg["bonus_sla"], "bonus",
               f"Bônus SLA missão #{t['id']}: {liq:.0f} min líquidos ≤ {sla} min (impedimentos externos descontados)")
        total += rg["bonus_sla"]
    if t["reaberturas"] == 0:
        lancar(c, t["executor_id"], t["id"], rg["bonus_sem_reabertura"], "bonus",
               f"Bônus qualidade missão #{t['id']}: resolvida sem reabertura")
        total += rg["bonus_sem_reabertura"]
    return int(round(total))


def ranking(c, temporada, setor_id=None):
    filtro = " AND e.setor_id=?" if setor_id else ""
    params = [temporada] + ([setor_id] if setor_id else [])
    individual = c.execute(
        "SELECT u.id, u.nome, u.avatar, e.nome equipe, "
        "coalesce(sum(CASE WHEN p.tipo IN ('definitivo','bonus') THEN p.pontos END),0) pontos, "
        "coalesce(sum(CASE WHEN p.tipo='provisorio' THEN p.pontos END),0) provisorios "
        "FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id "
        "LEFT JOIN pontos p ON p.usuario_id=u.id AND p.temporada=? "
        f"WHERE (u.papeis LIKE '%executor%' OR u.papeis LIKE '%gestor%'){filtro} "
        "GROUP BY u.id ORDER BY pontos DESC, u.nome", params).fetchall()
    equipes = c.execute(
        "SELECT e.id, e.nome, s.nome setor, coalesce(sum(CASE WHEN p.tipo IN ('definitivo','bonus') THEN p.pontos END),0) pontos "
        "FROM equipes e JOIN setores s ON s.id=e.setor_id LEFT JOIN pontos p ON p.equipe_id=e.id AND p.temporada=? "
        f"WHERE 1=1{filtro} GROUP BY e.id ORDER BY pontos DESC", params).fetchall()
    return individual, equipes


def medalhas(c, uid):
    m = []
    if c.execute("SELECT 1 FROM pontos WHERE usuario_id=? AND tipo='definitivo' AND tarefa_id IS NOT NULL", (uid,)).fetchone():
        m.append(("target", "Primeira missão confirmada"))
    if c.execute("SELECT 1 FROM tarefas WHERE executor_id=? AND status='concluido' AND nota=5", (uid,)).fetchone():
        m.append(("star", "Cinco estrelas"))
    if c.execute("SELECT 1 FROM tarefas WHERE executor_id=? AND status='concluido' AND tipo_id=1", (uid,)).fetchone():
        m.append(("network", "Mestre das Redes"))
    if c.execute("SELECT 1 FROM pontos WHERE usuario_id=? AND descricao LIKE 'Apoio%'", (uid,)).fetchone():
        m.append(("handshake", "Parceiro de equipe"))
    if c.execute("SELECT 1 FROM pontos WHERE usuario_id=? AND descricao LIKE 'Desbloqueio%'", (uid,)).fetchone():
        m.append(("lock-open", "Destravador"))
    return m


def nivel(pontos_totais):
    for limite, nome in ((600, "Lenda de SJP"), (300, "Guardião"), (100, "Explorador")):
        if pontos_totais >= limite:
            return nome
    return "Recruta"


# ---------------------------------------------------------------- indicadores

def indicadores(c, setor_id=None):
    w, wa, p = (" WHERE t.setor_id=?", " AND t.setor_id=?", (setor_id,)) if setor_id else ("", "", ())
    por_status = {r["status"]: r["n"] for r in c.execute(f"SELECT t.status, count(*) n FROM tarefas t{w} GROUP BY t.status", p)}
    tempos_inicio, tempos_conf = [], []
    for r in c.execute(
        "SELECT (SELECT min(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='encaminhado') a, "
        "(SELECT min(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='em_execucao') e, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='executado') c, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='concluido') r FROM tarefas t" + w, p):
        if r["a"] and r["e"]:
            tempos_inicio.append(minutos(r["a"], r["e"]))
        if r["c"] and r["r"]:
            tempos_conf.append(minutos(r["c"], r["r"]))
    media = lambda xs: f"{sum(xs) / len(xs):.0f} min" if xs else "–"  # noqa: E731
    csat = c.execute("SELECT avg(t.nota) m, count(t.nota) n FROM tarefas t WHERE t.nota IS NOT NULL" + wa, p).fetchone()
    motivos = c.execute("SELECT i.motivo, count(*) n FROM impedimentos i JOIN tarefas t ON t.id=i.tarefa_id" + w +
                        " GROUP BY i.motivo ORDER BY n DESC LIMIT 3", p).fetchall()
    return {
        "recebidas": sum(por_status.values()),
        "novos": sum(por_status.get(s, 0) for s in FILA_ATENDIMENTO),
        "em_execucao": sum(por_status.get(s, 0) for s in ("encaminhado", "a_caminho", "em_execucao")),
        "devolvidos": por_status.get("devolvido", 0),
        "cancelados": por_status.get("cancelado", 0),
        "impedidas": por_status.get("impedido", 0),
        "aguardando": por_status.get("executado", 0),
        "resolvidas": por_status.get("concluido", 0),
        "reaberturas": c.execute("SELECT coalesce(sum(t.reaberturas),0) n FROM tarefas t" + w, p).fetchone()["n"],
        "tempo_inicio": media(tempos_inicio),
        "tempo_confirmacao": media(tempos_conf),
        "csat": f"{csat['m']:.1f}★ ({csat['n']})" if csat["n"] else "–",
        "motivos": motivos,
        "outbox_pendente": 0 if setor_id else c.execute("SELECT count(*) n FROM outbox WHERE status='pendente'").fetchone()["n"],
        "auto_resolvidos": 0 if setor_id else contar_auto_resolvidos(c),
        "auto_ia_resolvidos": 0 if setor_id else contar_auto_resolvidos(c, somente_ia=True),
        "solicitacoes_aguardando": 0 if setor_id else c.execute("SELECT count(*) n FROM solicitacoes WHERE status='aguardando'").fetchone()["n"],
    }


# ---------------------------------------------------------------- métricas no tempo (painel do gestor)

def _data(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _escala(maximo):
    """Topo "redondo" do eixo e 4 marcas (0, ¼, ½, ¾, topo)."""
    passo = 1
    for p in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        passo = p
        if maximo <= p * 4:
            break
    return [passo * i for i in range(5)]


def tarefas_do_periodo(c, inicio, setor_id=None):
    filtro = " AND t.setor_id=?" if setor_id else ""
    return c.execute(
        "SELECT t.*, tp.nome tipo, tp.base_conhecimento kb, u.nome executor, u.avatar, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='concluido') resolvida_em "
        "FROM tarefas t LEFT JOIN tipos tp ON tp.id=t.tipo_id LEFT JOIN usuarios u ON u.id=t.executor_id "
        f"WHERE t.criado_em>=?{filtro}", [inicio] + ([setor_id] if setor_id else [])).fetchall()


def metricas(c, dias, setor_id=None):
    hoje = datetime.now()
    seg = (hoje - timedelta(days=hoje.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    n_sem = max(2, -(-dias // 7))
    semanas = [seg - timedelta(weeks=i) for i in range(n_sem - 1, -1, -1)]
    inicio = semanas[0].strftime("%Y-%m-%d %H:%M:%S")
    tarefas = tarefas_do_periodo(c, inicio, setor_id)
    rg = regras(c)

    def semana_de(s):
        d = _data(s)
        return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0)

    por_sem = {s: {"abertos": 0, "resolvidos": 0, "horas": []} for s in semanas}
    resolvidas, no_sla, com_sla, concordam, com_ia = [], 0, 0, 0, 0
    for t in tarefas:
        if (s := semana_de(t["criado_em"])) in por_sem:
            por_sem[s]["abertos"] += 1
        if t["resolvida_em"]:
            h = (_data(t["resolvida_em"]) - _data(t["criado_em"])).total_seconds() / 3600
            resolvidas.append((t, h))
            if (s := semana_de(t["resolvida_em"])) in por_sem:
                por_sem[s]["resolvidos"] += 1
                por_sem[s]["horas"].append(h)
            liq = tempo_liquido(c, t)
            if liq is not None:
                com_sla += 1
                no_sla += liq <= rg["sla_minutos"].get(t["prioridade"] or "P3", 1440)
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        if ia.get("prioridade") and t["prioridade"]:
            com_ia += 1
            concordam += ia["prioridade"] == t["prioridade"]

    media = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    serie = [{"rotulo": s.strftime("%d/%m"), "abertos": v["abertos"], "resolvidos": v["resolvidos"],
              "horas": media(v["horas"])} for s, v in por_sem.items()]

    # gráfico de linhas abertos × resolvidos (mesma unidade → um só eixo)
    W, H, ml, mr, mt, mb = 640, 220, 36, 16, 12, 28
    ticks = _escala(max([x["abertos"] for x in serie] + [x["resolvidos"] for x in serie] + [1]))
    passo_x = (W - ml - mr) / max(1, len(serie) - 1)
    y = lambda v: mt + (1 - v / ticks[-1]) * (H - mt - mb)  # noqa: E731
    for i, x in enumerate(serie):
        x["x"], x["y_ab"], x["y_res"] = ml + i * passo_x, y(x["abertos"]), y(x["resolvidos"])
    linhas = {"W": W, "H": H, "ml": ml, "mr": mr, "mt": mt, "mb": mb, "passo": passo_x,
              "ticks": [(v, y(v)) for v in ticks],
              "abertos": " ".join(f"{x['x']:.1f},{x['y_ab']:.1f}" for x in serie),
              "resolvidos": " ".join(f"{x['x']:.1f},{x['y_res']:.1f}" for x in serie)}

    # colunas: tempo médio de resolução por semana (horas)
    ticks_h = _escala(max([x["horas"] or 0 for x in serie] + [1]))
    yh = lambda v: mt + (1 - v / ticks_h[-1]) * (H - mt - mb)  # noqa: E731
    banda = (W - ml - mr) / len(serie)
    larg = min(24, banda * 0.6)
    for i, x in enumerate(serie):
        x["cx"] = ml + i * banda + (banda - larg) / 2
        x["cy"] = yh(x["horas"] or 0)
    colunas = {"W": W, "H": H, "ml": ml, "mt": mt, "mb": mb, "banda": banda, "larg": larg,
               "base": H - mb, "ticks": [(v, yh(v)) for v in ticks_h]}

    # por secretaria e matriz secretaria × tipo
    sec = {}
    for t in tarefas:
        nome = t["secretaria"] or "Não informada"
        d = sec.setdefault(nome, {"nome": nome, "n": 0, "notas": [], "horas": [], "tipos": {}})
        d["n"] += 1
        d["tipos"][t["tipo"] or "Sem tipo"] = d["tipos"].get(t["tipo"] or "Sem tipo", 0) + 1
        if t["nota"]:
            d["notas"].append(t["nota"])
    for t, h in resolvidas:
        sec[t["secretaria"] or "Não informada"]["horas"].append(h)
    secretarias = sorted(sec.values(), key=lambda d: -d["n"])
    for d in secretarias:
        d["csat"], d["horas_media"] = media(d["notas"]), media(d["horas"])
    tipos = sorted({tp for d in secretarias for tp in d["tipos"]},
                   key=lambda tp: -sum(d["tipos"].get(tp, 0) for d in secretarias))
    max_cel = max([n for d in secretarias for n in d["tipos"].values()] + [1])

    # produtividade por executor (resolvidas no período)
    exe = {}
    km_por = {}
    for t in tarefas:
        if t["executor_id"] and t["km_percorrido"]:
            km_por[t["executor_id"]] = km_por.get(t["executor_id"], 0) + t["km_percorrido"]
    for t, h in resolvidas:
        if not t["executor_id"]:
            continue
        d = exe.setdefault(t["executor_id"], {"nome": t["executor"], "avatar": t["avatar"], "n": 0, "horas": [],
                                              "notas": [], "reab": 0})
        d["n"] += 1
        d["horas"].append(h)
        d["reab"] += t["reaberturas"]
        if t["nota"]:
            d["notas"].append(t["nota"])
    todos_exec = carga_executores(c, setor_id, todos=True)
    ativos = {r["id"]: r["carga"] for r in todos_exec}
    for r in todos_exec:  # técnicos sem resolução no período também aparecem (km e disponibilidade)
        exe.setdefault(r["id"], {"nome": r["nome"], "avatar": r["avatar"], "n": 0, "horas": [], "notas": [], "reab": 0})
    disp = {r["id"]: r["disponivel"] for r in todos_exec}
    executores = sorted(({**d, "id": k, "horas_media": media(d["horas"]), "csat": media(d["notas"]),
                          "ativos": ativos.get(k, 0), "km": round(km_por.get(k, 0), 1), "disponivel": disp.get(k, 1)}
                         for k, d in exe.items()), key=lambda d: -d["n"])
    max_exec = max([d["n"] for d in executores] + [1])

    notas = [t["nota"] for t, _ in resolvidas if t["nota"]]
    return {
        "dias": dias, "inicio": semanas[0].strftime("%d/%m/%Y"), "serie": serie, "linhas": linhas, "colunas": colunas,
        "abertos": len(tarefas), "resolvidos": len(resolvidas),
        "cancelados": sum(1 for t in tarefas if t["status"] == "cancelado"),
        "nivel1": (100 * sum(1 for t, _ in resolvidas if t["resolvido_atendimento_por"]) / len(resolvidas)) if resolvidas else None,
        "devolvidos": c.execute("SELECT count(DISTINCT tarefa_id) n FROM eventos WHERE para='devolvido' AND criado_em>=?",
                                (inicio,)).fetchone()["n"],
        "horas_media": media([h for _, h in resolvidas]),
        "sla": (100 * no_sla / com_sla) if com_sla else None,
        "csat": media(notas), "n_notas": len(notas),
        "reabertura": (100 * sum(1 for t, _ in resolvidas if t["reaberturas"]) / len(resolvidas)) if resolvidas else None,
        "concordancia_ia": (100 * concordam / com_ia) if com_ia else None, "ajustes_ia": com_ia - concordam,
        "auto_resolvidos": contar_auto_resolvidos(c, inicio),
        "auto_ia_resolvidos": contar_auto_resolvidos(c, inicio, somente_ia=True),
        "secretarias": secretarias, "max_sec": max([d["n"] for d in secretarias] + [1]),
        "tipos": tipos, "max_cel": max_cel, "executores": executores, "max_exec": max_exec,
    }


def dados_para_capacitacao(c, dias):
    """Recorrência por secretaria × tipo com exemplos reais de títulos: matéria-prima da análise de capacitação."""
    inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    grupos = {}
    for t in tarefas_do_periodo(c, inicio):
        chave = (t["secretaria"] or "Não informada", t["tipo"] or "Sem tipo")
        g = grupos.setdefault(chave, {"secretaria": chave[0], "tipo": chave[1], "kb": t["kb"] or "", "chamados": 0,
                                      "reaberturas": 0, "exemplos": []})
        g["chamados"] += 1
        g["reaberturas"] += t["reaberturas"]
        if t["titulo"] not in g["exemplos"] and len(g["exemplos"]) < 5:
            g["exemplos"].append(t["titulo"])
    return sorted(grupos.values(), key=lambda g: -g["chamados"])


# Temas que o próprio servidor resolve com orientação (treinamento/tutorial) × temas que pedem ação de infraestrutura.
ACOES_POR_TEMA = {
    "Impressora": ("Oficina rápida", "Uso da impressora: papel atolado, toner e reinício seguro"),
    "Computador": ("Tutorial em vídeo", "Primeiros passos quando o computador fica lento ou não liga"),
    "Senha": ("Tutorial + comunicado", "Senhas: como evitar bloqueios e usar a redefinição de senha"),
    "Sistema": ("Treinamento no sistema", "Uso correto do sistema municipal e como registrar erros com print"),
    "Wi-fi": ("Tutorial em vídeo", "Conectar e reconectar à rede Wi-fi institucional"),
    "Rede": ("Guia ilustrado", "Checagem do cabo e da tomada de rede antes de abrir chamado"),
    "Telefonia": ("Guia ilustrado", "Uso do ramal e do celular corporativo: testes simples antes de abrir chamado"),
    "Backup": ("Comunicado + orientação", "Onde salvar arquivos para estarem protegidos pelo backup"),
    "Datacenter": ("Manutenção preventiva", "Monitoramento e janela de manutenção dos sistemas mais críticos"),
}


def capacitacao_por_regras(grupos, dias):
    acoes = []
    for g in grupos:
        if g["chamados"] < 3 or len(acoes) >= 6:
            continue
        formato, tema = ACOES_POR_TEMA.get(g["kb"], ("Orientação dirigida", f"Boas práticas sobre {g['tipo'].lower()}"))
        infra = formato == "Manutenção preventiva"
        acoes.append({
            "secretaria": g["secretaria"], "tema": tema, "formato": formato,
            "publico": f"Unidades da secretaria de {g['secretaria']}" if infra else f"Servidores da secretaria de {g['secretaria']}",
            "evidencia": f"{g['chamados']} chamados de {g['tipo']} em {dias} dias",
            "justificativa": ("Demanda recorrente de infraestrutura: prevenção reduz chamados corretivos."
                              if infra else "Problema recorrente que o próprio servidor pode resolver com orientação simples."),
            "impacto": "alto" if g["chamados"] >= 10 else "médio" if g["chamados"] >= 5 else "baixo",
            "chamados_evitaveis_mes": round(g["chamados"] * 30 / dias * (0.3 if infra else 0.5), 1),
        })
    total = sum(g["chamados"] for g in grupos)
    resumo = (f"{total} chamados em {dias} dias. As maiores recorrências estão em "
              + ", ".join(f"{g['tipo']} na {g['secretaria']} ({g['chamados']})" for g in grupos[:3]) + ".") if grupos else \
        "Sem chamados no período."
    return {"resumo": resumo, "acoes": acoes}


def gerar_analise_capacitacao(dias):
    with db() as c:
        grupos = dados_para_capacitacao(c, dias)
        auto = c.execute("SELECT count(*) n FROM autoatendimento WHERE resolvido=1").fetchone()["n"]
    fonte = "llm"
    try:
        r = extrair_json(llm([
            {"role": "system", "content": "Você é analista de gestão de serviços de uma prefeitura. Responda apenas JSON válido."},
            {"role": "user", "content":
             "Com base na recorrência de chamados por secretaria e tipo, proponha ações MASSIVAS de capacitação ou "
             "prevenção que reduzam o volume futuro de chamados (treinamentos, oficinas, tutoriais em vídeo, guias "
             "ilustrados, comunicados, manutenção preventiva, novos conteúdos para o assistente virtual de "
             "autoatendimento). Priorize o que o próprio servidor consegue resolver com orientação; para problemas de "
             "infraestrutura, proponha prevenção em vez de treinamento. Baseie-se só nos dados; não invente números.\n"
             'Responda SOMENTE um JSON com: "resumo" (2-3 frases com o diagnóstico geral) e "acoes" (lista de 3 a 6, '
             'ordenada por impacto, cada uma com "secretaria", "tema" (título curto da ação), "formato", "publico", '
             '"evidencia" (os números que justificam), "justificativa" (1 frase), "impacto" ("alto"|"médio"|"baixo"), '
             '"chamados_evitaveis_mes" (número estimado, conservador)).\n'
             f"PERÍODO: últimos {dias} dias. Conversas resolvidas pelo assistente virtual (total): {auto}.\n"
             f"RECORRÊNCIA (secretaria × tipo, com títulos de exemplo): "
             f"{json.dumps([{k: g[k] for k in ('secretaria', 'tipo', 'chamados', 'reaberturas', 'exemplos')} for g in grupos[:25]], ensure_ascii=False)}"}],
            max_tokens=2500, temperature=0.3))
        if not isinstance(r.get("acoes"), list) or not r["acoes"]:
            raise ValueError("JSON fora do esperado")
        r["acoes"] = [a for a in r["acoes"] if isinstance(a, dict)][:6]
    except Exception as e:  # noqa: BLE001 — fallback garante a análise sem IA externa
        log.warning("análise de capacitação LLM falhou (%s); usando regras", e)
        r, fonte = capacitacao_por_regras(grupos, dias), "regras"
    with db() as c:
        set_cfg(c, "analise_capacitacao", r | {"fonte": fonte, "dias": dias, "gerada_em": agora(), "status": "pronta"})


# ---------------------------------------------------------------- push (Web Push / VAPID auto-hospedado)

def _vapid_derivada():
    """Demo pública: a chave VAPID deriva do segredo da sessão, igual em todas as instâncias."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    ordem = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    d = int.from_bytes(hashlib.sha256(("vapid:" + SECRET).encode()).digest(), "big") % (ordem - 1) + 1
    pem = ec.derive_private_key(d, ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    if not VAPID_PEM.exists() or VAPID_PEM.read_bytes() != pem:
        VAPID_PEM.parent.mkdir(parents=True, exist_ok=True)
        VAPID_PEM.write_bytes(pem)


def chave_publica_vapid():
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    if os.getenv("DEMO_DATA_DIR"):
        _vapid_derivada()
    elif not VAPID_PEM.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(VAPID_PEM))
    v = Vapid.from_file(str(VAPID_PEM))
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def enviar_push(usuario_id, titulo, corpo, url):
    from pywebpush import WebPushException, webpush
    with db() as c:
        subs = c.execute("SELECT * FROM push_subs WHERE usuario_id=?", (usuario_id,)).fetchall()
    for s in subs:
        try:
            webpush(subscription_info=json.loads(s["sub_json"]),
                    data=json.dumps({"title": titulo, "body": corpo, "url": url}, ensure_ascii=False),
                    vapid_private_key=str(VAPID_PEM), vapid_claims={"sub": VAPID_SUB}, timeout=10)
            log.info("push enviado ao usuário %s", usuario_id)
        except WebPushException as e:
            log.warning("push falhou: %s", e)
            if e.response is not None and e.response.status_code in (404, 410):
                with db() as c:
                    c.execute("DELETE FROM push_subs WHERE id=?", (s["id"],))
        except Exception as e:  # noqa: BLE001
            log.warning("push falhou: %s", e)


# ---------------------------------------------------------------- app, sessão e permissões

@asynccontextmanager
async def lifespan(_app):
    init_db()
    chave_publica_vapid()

    async def laco_outbox():
        while True:
            await asyncio.to_thread(processar_outbox)
            await asyncio.sleep(5)

    tarefa = asyncio.create_task(laco_outbox())
    yield
    tarefa.cancel()


app = FastAPI(title="Missões SJP – API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["upload_existe"] = lambda nome: bool(nome) and (UPLOADS / Path(str(nome)).name).is_file()
templates.env.globals.update(STATUS=STATUS, PRIORIDADES=PRIORIDADES, MOTIVOS=MOTIVOS_IMPEDIMENTO, MOTIVOS_DEVOLUCAO=MOTIVOS_DEVOLUCAO,
                             STATUS_OFICIAL=STATUS_OFICIAL, STATUS_OFICIAIS=STATUS_OFICIAIS,
                             CANCELAVEIS=[s for s, prox in TRANSICOES.items() if "cancelado" in prox],
                             SISTEMAS=SISTEMAS_ORIGEM, LLM_MODEL=LLM_MODEL,
                             SECRETARIAS=SECRETARIAS)
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
templates.env.filters["hora"] = lambda s: s[11:16] if s else ""
templates.env.filters["datahora"] = lambda s: f"{s[8:10]}/{s[5:7]} {s[11:16]}" if s else ""
templates.env.filters["dec"] = lambda v, casas=1: "–" if v is None else f"{v:.{casas}f}".replace(".", ",")
templates.env.filters["espera"] = lambda m: (f"{m:.0f} min" if m < 60 else f"{m / 60:.0f} h" if m < 48 * 60
                                            else f"{m / 1440:.0f} dias")
templates.env.filters["duracao"] = lambda h: "–" if h is None else (f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} dias").replace(".", ",")


# Páginas já migradas para a interface DataForge (templates/df/). As demais continuam em Pico até migrarem.
DF_PAGINAS = {p.name for p in (BASE / "templates" / "df").glob("*.html")} - {"_base.html", "_ui.html", "erro.html"}


def render(request, nome, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("solicitante_demo", perfil_solicitante_demo(request))
    if nome == "login.html":
        ctx.setdefault("perfil_demo", SOLICITANTE_DEMO)
    if nome in DF_PAGINAS:
        nome = f"df/{nome}"
    return templates.TemplateResponse(request, nome, ctx)


def assinar(uid):
    return f"{uid}.{hmac.new(SECRET.encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:32]}"


# Identidade fixa e fictícia para demonstrar a entrada do solicitante.
SOLICITANTE_DEMO = {"nome": "Ana Souza", "email": "ana.souza.demo@example.com",
                    "secretaria": "Saúde", "contato": "2231"}


def recuperar_acesso_demo(request, c):
    """Só a identidade fictícia assinada pode recompor uma sessão perdida na demonstração."""
    cookie = request.cookies.get("solicitante_demo", "")
    if not cookie or len(cookie) > 300 or "." not in cookie:
        return None
    token = request.cookies.get("acesso_solicitante")
    if not token:
        return None
    payload = cookie.rsplit(".", 1)[0]
    if not hmac.compare_digest(cookie, assinar(payload)):
        return None
    acesso = c.execute("SELECT email, criado_em FROM acessos_solicitante WHERE token=?", (token,)).fetchone()
    # Cookies anteriores continuam válidos somente quando o registro ainda existe.
    if payload == "solicitante-demo":
        pass
    else:
        try:
            tipo, assinado_token, expires = payload.split(":")
            expires = int(expires)
            if tipo != "solicitante-demo" or assinado_token != token or expires <= datetime.now().timestamp():
                return None
        except (ValueError, TypeError):
            return None
        if not acesso:
            criado = datetime.fromtimestamp(expires) - timedelta(days=ACESSO_DIAS)
            c.execute("INSERT OR IGNORE INTO acessos_solicitante(token,email,criado_em) VALUES(?,?,?)",
                      (token, SOLICITANTE_DEMO["email"], criado.strftime("%Y-%m-%d %H:%M:%S")))
            acesso = c.execute("SELECT email, criado_em FROM acessos_solicitante WHERE token=?", (token,)).fetchone()
    if not acesso or acesso["email"] != SOLICITANTE_DEMO["email"] or _data(acesso["criado_em"]) < datetime.now() - timedelta(days=ACESSO_DIAS):
        return None
    return SOLICITANTE_DEMO


def perfil_solicitante_demo(request):
    with db() as c:
        return recuperar_acesso_demo(request, c)


def usuario_do_cookie(request, nome):
    v = request.cookies.get(nome)
    if not v or "." not in v or not hmac.compare_digest(assinar(v.split(".", 1)[0]), v):
        return None
    with db() as c:
        return c.execute("SELECT u.*, e.nome equipe FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id "
                         "WHERE u.id=?", (v.split(".", 1)[0],)).fetchone()


def para_login(request, cookie):
    """Sessão ausente: vai ao login; se havia cookie inválido ou expirado, avisa."""
    aviso = "?msg=" + quote("Sua sessão expirou. Entre novamente.") if request.cookies.get(cookie) else ""
    return HTTPException(303, headers={"Location": "/login" + aviso})


def exige_painel(request, *papeis):
    u = usuario_do_cookie(request, "sess_painel")
    if not u:
        raise para_login(request, "sess_painel")
    if papeis and not set(papeis) & set(u["papeis"].split(",")):
        raise HTTPException(403, f"Acesso restrito ao(s) papel(is): {', '.join(papeis)}")
    return u


def eh_gestor_tecnico(u):
    return "gestor_tecnico" in u["papeis"].split(",")


def exige_acesso(u, t):
    """O gestor técnico só enxerga e age nos chamados direcionados para a sua área."""
    if eh_gestor_tecnico(u) and t["setor_id"] != u["area_id"]:
        raise HTTPException(403, "Este chamado pertence a outra área técnica.")


def exige_executor(request):
    u = usuario_do_cookie(request, "sess_campo")
    if not u:
        raise para_login(request, "sess_campo")
    return u


def papel_painel(u):
    return "gestor" if "gestor" in u["papeis"] else "atendente"


def tarefa_ou_404(c, tid):
    t = c.execute("SELECT * FROM tarefas WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "Tarefa não encontrada")
    return t


def redirect(url):
    caminho, sep, msg = url.partition("?msg=")
    return RedirectResponse(caminho + sep + quote(msg), status_code=303)


def recarregar_se_mudou(c, tid, versao):
    t = tarefa_ou_404(c, tid)
    if t["atualizado_em"] != versao:
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return Response(status_code=204)


ERROS_UI = {403: ("prohibit", "Acesso restrito", "/login", "Trocar de usuário"),
            404: ("magnifying-glass", "Página não encontrada", "/", "Ir ao início"),
            409: ("warning", "Ação não permitida agora", "/", "Ir ao início"),
            422: ("warning", "Revise os dados enviados", "/", "Ir ao início")}


def pagina_de_erro(request, codigo, detalhe):
    icone, titulo, destino, rotulo = ERROS_UI.get(codigo, ("warning", "Não foi possível concluir", "/", "Ir ao início"))
    if codigo >= 500:
        detalhe = "Algo falhou no servidor. Tente novamente em instantes."
    u = usuario_do_cookie(request, "sess_painel")
    perfil = "painel"
    if not u:
        u = usuario_do_cookie(request, "sess_campo")
        perfil = "campo" if u else "publico"
    return templates.TemplateResponse(request, "df/erro.html",
                                      dict(codigo=codigo, icone=icone, titulo=titulo, detalhe=detalhe, destino=destino,
                                           destino_rotulo=rotulo, u=u, perfil=perfil, ativo="", msg=None),
                                      status_code=codigo)


@app.exception_handler(StarletteHTTPException)
async def erro_http(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"erro": exc.detail}, status_code=exc.status_code)
    return pagina_de_erro(request, exc.status_code, str(exc.detail))


# ---------------------------------------------------------------- login

@app.get("/", include_in_schema=False)
def inicio(request: Request):
    u = usuario_do_cookie(request, "sess_painel")
    if u:
        return redirect("/gestor" if "gestor" in u["papeis"] else "/atendente")
    return redirect("/campo" if usuario_do_cookie(request, "sess_campo") else "/login")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    with db() as c:
        garantir_gestor_tecnico(c)
        usuarios = c.execute("SELECT u.*, e.nome equipe FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id ORDER BY u.id").fetchall()
        areas = {r["id"]: r["nome"] for r in c.execute("SELECT id, nome FROM setores")}
    return render(request, "login.html", usuarios=usuarios, areas=areas,
                  painel=usuario_do_cookie(request, "sess_painel"), campo=usuario_do_cookie(request, "sess_campo"))


@app.post("/login")
def login(uid: int = Form(...)):
    if uid == 0:
        return login_solicitante_demo()
    with db() as c:
        garantir_gestor_tecnico(c)
        u = c.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    executor = "executor" in u["papeis"]
    resp = redirect("/campo" if executor else ("/gestor" if "gestor" in u["papeis"] else "/atendente"))
    # Sessões separadas para painel (web) e campo (mobile): permite demonstrar gestor e executor lado a lado.
    resp.set_cookie("sess_campo" if executor else "sess_painel", assinar(u["id"]), httponly=True, samesite="lax")
    return resp


@app.post("/login/solicitante")
def login_solicitante_demo():
    """Acesso de demonstração restrito à identidade fictícia; não autentica pessoas reais."""
    token = secrets.token_urlsafe(16)
    with db() as c:
        c.execute("INSERT INTO acessos_solicitante(token,email,criado_em) VALUES(?,?,?)",
                  (token, SOLICITANTE_DEMO["email"], agora()))
    resp = redirect("/servidor")
    expires = int((datetime.now() + timedelta(days=ACESSO_DIAS)).timestamp())
    for nome, valor in (("acesso_solicitante", token),
                        ("solicitante_demo", assinar(f"solicitante-demo:{token}:{expires}"))):
        resp.set_cookie(nome, valor, httponly=True, samesite="lax", max_age=ACESSO_DIAS * 86400)
    return resp


@app.get("/sair")
def sair(area: str = "painel"):
    resp = redirect("/login")
    resp.delete_cookie("sess_campo" if area == "campo" else "sess_painel")
    return resp


# ---------------------------------------------------------------- integração (Req. 2)

@app.post("/api/integracao/{origem}/tarefas", tags=["integração"])
def receber_tarefa(origem: str, dados: dict, x_api_key: str = Header(...)):
    """Recebe tarefa de um sistema de origem. Idempotente por (origem, external_id)."""
    sistema = SISTEMAS_ORIGEM.get(origem)
    if not sistema or not hmac.compare_digest(x_api_key, sistema["api_key"]):
        raise HTTPException(401, "Sistema de origem ou chave inválidos")
    for campo in ("external_id", "descricao"):
        if not str(dados.get(campo, "")).strip():
            raise HTTPException(422, f"Campo obrigatório: {campo}")
    with db() as c:
        existente = c.execute("SELECT id FROM tarefas WHERE origem=? AND external_id=?",
                              (origem, str(dados["external_id"]))).fetchone()
        if existente:
            return JSONResponse({"id": existente["id"], "duplicada": True,
                                 "mensagem": "Demanda já recebida; nenhum registro novo criado."}, status_code=200)
        ts = agora()
        cur = c.execute(
            "INSERT INTO tarefas(origem,external_id,titulo,descricao,local,solicitante,contato,email,secretaria,setor_id,"
            "token,criado_em,atualizado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (origem, str(dados["external_id"]), (dados.get("titulo") or dados["descricao"])[:80], dados["descricao"],
             dados.get("local", ""), dados.get("solicitante", ""), dados.get("contato", ""), dados.get("email", ""),
             dados.get("secretaria", ""), sistema["setor_id"], secrets.token_urlsafe(16), ts, ts))
        tid = cur.lastrowid
        registrar_evento(c, tid, sistema["nome"], "integracao",
                         f"Demanda {dados['external_id']} recebida via API (registrada por {dados.get('atendente', 'atendente')})",
                         None, "novo")
        enfileirar_outbox(c, tarefa_ou_404(c, tid), "novo", f"Recebida na plataforma Missões SJP como tarefa #{tid}",
                          "Missões SJP")
    em_segundo_plano(triar, tid)
    em_segundo_plano(processar_outbox)
    return JSONResponse({"id": tid, "duplicada": False}, status_code=201)


@app.post("/integracoes/reprocessar")
def reprocessar(request: Request):
    exige_painel(request, "gestor")
    processar_outbox()
    return redirect("/config?msg=Fila de integração reprocessada")


# ---------------------------------------------------------------- atendente e gestor (web)

@app.get("/atendente", response_class=HTMLResponse)
def atendente(request: Request, sol: int = 0):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        setores = c.execute("SELECT * FROM setores").fetchall()
        minhas = c.execute("SELECT * FROM tarefas ORDER BY criado_em DESC, id DESC LIMIT 15").fetchall()
        solicitacoes = c.execute("SELECT * FROM solicitacoes WHERE status='aguardando' ORDER BY id").fetchall()
    sel = next((s for s in solicitacoes if s["id"] == sol), None)
    return render(request, "atendente.html", u=u, setores=setores, tarefas=minhas, solicitacoes=solicitacoes,
                  sel=sel, secretarias=SECRETARIAS)


def fila_atendimento(c):
    """Chamados que aguardam avaliação do atendimento: novos, devolvidos pelo técnico e reabertos pela demandante."""
    fila = c.execute(
        "SELECT t.*, (SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para=t.status) entrou_em "
        f"FROM tarefas t WHERE t.status IN ({','.join('?' * len(FILA_ATENDIMENTO))})", FILA_ATENDIMENTO).fetchall()
    itens = []
    for t in fila:
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        prio = t["prioridade"] or ia.get("prioridade") or "P3"
        desde = t["entrou_em"] or t["criado_em"]
        itens.append({"t": t, "ia": ia, "prio": prio, "minutos": minutos(desde, agora())})
    # mais crítico primeiro; empate: quem espera há mais tempo
    return sorted(itens, key=lambda x: (x["prio"], -x["minutos"]))


@app.get("/atendente/fila", response_class=HTMLResponse)
def atendente_fila(request: Request, compacta: int = 0, atual: int = 0):
    exige_painel(request, "atendente", "gestor")
    with db() as c:
        fila = fila_atendimento(c)
        tipos = {r["id"]: r for r in c.execute("SELECT t.*, s.nome area FROM tipos t JOIN setores s ON s.id=t.setor_id")}
        execs = {r["id"]: r["nome"] for r in c.execute("SELECT id, nome FROM usuarios")}
    return render(request, "_fila.html", fila=fila, tipos=tipos, execs=execs, compacta=compacta, atual=atual)


def validar_solicitante(nome, email, secretaria):
    if not nome.strip():
        raise HTTPException(422, "Informe o nome de quem está abrindo o chamado")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(422, "Informe um e-mail válido")
    if secretaria not in SECRETARIAS:
        raise HTTPException(422, "Secretaria inválida")
    return nome.strip(), email.strip().lower(), secretaria


@app.post("/tarefas")
def criar_tarefa(request: Request, titulo: str = Form(...), descricao: str = Form(...), local: str = Form(""),
                 solicitante: str = Form(...), email: str = Form(...), secretaria: str = Form(...),
                 contato: str = Form(""), setor_id: int = Form(...)):
    """Cadastro direto na web, para setores sem sistema legado (Req. 1)."""
    u = exige_painel(request, "atendente", "gestor")
    solicitante, email, secretaria = validar_solicitante(solicitante, email, secretaria)
    with db() as c:
        ts = agora()
        cur = c.execute("INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,contato,email,secretaria,setor_id,"
                        "criado_por,token,criado_em,atualizado_em) VALUES('web',?,?,?,?,?,?,?,?,?,?,?,?)",
                        (titulo, descricao, local, solicitante, contato, email, secretaria, setor_id, u["id"],
                         secrets.token_urlsafe(16), ts, ts))
        registrar_evento(c, cur.lastrowid, u["nome"], "atendente", "Tarefa cadastrada diretamente na plataforma",
                         None, "novo")
    em_segundo_plano(triar, cur.lastrowid)
    return redirect(f"/tarefa/{cur.lastrowid}?msg=Tarefa cadastrada. A IA está fazendo a triagem…")


@app.get("/gestor", response_class=HTMLResponse)
def gestor(request: Request):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    with db() as c:
        setores = c.execute("SELECT * FROM setores ORDER BY id").fetchall()
    return render(request, "gestor.html", u=u, setores=setores)


def _sem_acento(txt):
    return "".join(ch for ch in unicodedata.normalize("NFD", str(txt or "").lower()) if unicodedata.category(ch) != "Mn")


def _casa_busca(t, q):
    """Todos os termos devem aparecer em ID, título, descrição, local, solicitante, secretaria, e-mail, executor ou tipo."""
    campos = [f"#{t['id']}", str(t["id"]), t["external_id"], t["titulo"], t["descricao"], t["local"], t["solicitante"],
              t["secretaria"], t["email"], t["executor"], t["tipo"], t["prioridade"]]
    alvo = _sem_acento(" ".join(str(x) for x in campos if x))
    return all(_sem_acento(p.lstrip("#")) in alvo for p in q.split())


@app.get("/gestor/quadro", response_class=HTMLResponse)
def gestor_quadro(request: Request, setor_id: int | None = None, q: str = ""):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    if eh_gestor_tecnico(u):
        setor_id = u["area_id"]
    with db() as c:
        sql = ("SELECT t.*, u.nome executor, u.avatar, tp.nome tipo FROM tarefas t LEFT JOIN usuarios u ON u.id=t.executor_id "
               "LEFT JOIN tipos tp ON tp.id=t.tipo_id")
        tarefas = c.execute(sql + (" WHERE t.setor_id=?" if setor_id else "") + " ORDER BY coalesce(t.prioridade,'P9'), t.id",
                            (setor_id,) if setor_id else ()).fetchall()
        ind = indicadores(c, u["area_id"] if eh_gestor_tecnico(u) else None)
    q = q.strip()
    if q:  # busca somente entre os chamados em aberto
        tarefas = [t for t in tarefas if t["status"] not in FINAIS and _casa_busca(t, q)]
    colunas = {s: [t for t in tarefas if STATUS_OFICIAL[t["status"]] == s] for s in STATUS_OFICIAIS}
    totais = {s: len(ts) for s, ts in colunas.items()}
    # o histórico de encerrados cresce sem parar: o quadro mostra só os mais recentes (o resto está nas métricas)
    for s in FINAIS:
        colunas[s] = sorted(colunas[s], key=lambda t: t["atualizado_em"], reverse=True)[:10]
    impedidos = [t for t in tarefas if t["status"] == "impedido"]
    return render(request, "_quadro.html", colunas=colunas, totais=totais, impedidos=impedidos, ind=ind,
                  busca=q, n_busca=len(tarefas), so_area=eh_gestor_tecnico(u))


PERIODOS = {30: "Últimos 30 dias", 60: "Últimos 60 dias", 90: "Últimos 90 dias"}


# ---------------------------------------------------------------- painel da equipe técnica (backlog e proximidade)
def chave_local(t):
    """Chamados no mesmo local compartilham coordenadas (simuladas) e a mesma rota."""
    return (t["local"] or "").strip().lower() or t["id"]


def _posicao_do_tecnico(c, tid_ativos):
    """Posição atual: no local do chamado em execução, ou ao longo da rota se está a caminho; senão, na base."""
    for t in tid_ativos:
        if t["status"] == "em_execucao":
            return list(rastro_estado(chave_local(t), "em_execucao", "2000-01-01 00:00:00")["posicao"]), t
    for t in tid_ativos:
        if t["status"] == "a_caminho":
            ini = c.execute("SELECT criado_em FROM eventos WHERE tarefa_id=? AND para='a_caminho' ORDER BY id DESC LIMIT 1",
                            (t["id"],)).fetchone()
            est = rastro_estado(chave_local(t), "a_caminho", ini["criado_em"] if ini else None)
            if est:
                return list(est["posicao"]), t
    return list(BASE_RASTRO), None


def painel_equipe(c, u=None):
    tecnicos, ativos_por = [], {}
    for r in c.execute("SELECT t.*, tp.nome tipo FROM tarefas t LEFT JOIN tipos tp ON tp.id=t.tipo_id "
                       f"WHERE t.executor_id IS NOT NULL AND t.status IN ({','.join('?' * len(ATIVOS))}) "
                       "ORDER BY t.prioridade, t.id", ATIVOS):
        ativos_por.setdefault(r["executor_id"], []).append(r)
    area = u["area_id"] if u is not None and eh_gestor_tecnico(u) else None
    conf_por = {r["executor_id"]: r["n"] for r in c.execute(
        "SELECT executor_id, count(*) n FROM tarefas WHERE status='executado' AND executor_id IS NOT NULL GROUP BY executor_id")}
    for e in carga_executores(c, area, todos=True):
        ativas = ativos_por.get(e["id"], [])
        pos, atual = _posicao_do_tecnico(c, ativas)
        etapas = {s: sum(1 for t in ativas if t["status"] == s) for s in ATIVOS}
        tecnicos.append({
            "id": e["id"], "nome": e["nome"], "equipe": e["equipe"], "disponivel": bool(e["disponivel"]),
            "total": len(ativas), "etapas": etapas,
            "criticas": sum(1 for t in ativas if t["prioridade"] in ("P1", "P2")),
            "atual": atual, "posicao": pos, "ativas": ativas,
            "km_hoje": round(sum(t["km_percorrido"] or 0 for t in ativas), 1),
            "conf": conf_por.get(e["id"], 0), "equipe_id": e["equipe_id"],
        })
    maior = max([t["total"] for t in tecnicos] + [1])
    for t in tecnicos:
        t["pct"] = round(100 * t["total"] / maior)
        t["carga"] = t["total"] + t["conf"]
    maior_carga = max([t["carga"] for t in tecnicos] + [1])
    for t in tecnicos:
        t["pct_carga"] = round(100 * t["carga"] / maior_carga)
    por_eq = {}
    for t in tecnicos:
        d = por_eq.setdefault(t["equipe"], {"nome": t["equipe"], "chamados": 0, "membros": 0})
        d["chamados"] += t["carga"]
        d["membros"] += 1
    equipes = sorted(por_eq.values(), key=lambda d: -d["chamados"])
    maior_eq = max([d["chamados"] for d in equipes] + [1])
    for d in equipes:
        d["pct"] = round(100 * d["chamados"] / maior_eq)
    # chamados aguardando direcionamento, com sugestão por distância e carga
    pendentes = []
    itens = fila_atendimento(c) if area is None else []
    for t in c.execute("SELECT * FROM tarefas WHERE status='devolvido'" + (" AND setor_id=?" if area else ""),
                       (area,) if area else ()):
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        itens.append({"t": t, "ia": ia, "prio": t["prioridade"] or ia.get("prioridade") or "P3"})
    for item in itens:
        t = item["t"]
        alvo = rastro_destino(chave_local(t))
        ranking = sorted(({"id": x["id"], "nome": x["nome"], "total": x["total"], "km": round(km_entre(x["posicao"], alvo), 1),
                           "atual": x["atual"]} for x in tecnicos if x["disponivel"]), key=lambda x: (x["km"], x["total"]))
        pendentes.append({"t": t, "prio": item["prio"], "alvo": list(alvo), "ranking": ranking[:3],
                          "ia": item["ia"]})
    return {"tecnicos": tecnicos, "pendentes": pendentes, "equipes": equipes}


@app.get("/gestor/equipe", response_class=HTMLResponse)
def equipe_pagina(request: Request):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    return render(request, "equipe.html", u=u)


# ---------------------------------------------------------------- histórico de rotas executadas (gestor)
def historico_rotas(c, u, dias=7):
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    area = u["area_id"] if eh_gestor_tecnico(u) else None
    linhas = c.execute(
        "SELECT * FROM (SELECT t.id, t.titulo, t.local, t.km_percorrido km, t.executor_id, us.nome executor, t.setor_id, "
        "(SELECT min(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='a_caminho') saida, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='em_execucao') chegada "
        "FROM tarefas t JOIN usuarios us ON us.id=t.executor_id WHERE t.km_percorrido>0) x "
        "WHERE x.chegada>=?" + (" AND x.setor_id=?" if area else "") + " ORDER BY x.chegada DESC",
        (desde, area) if area else (desde,)).fetchall()
    por = {}
    for r in linhas:
        d = por.setdefault(r["executor_id"], {"nome": r["executor"], "km": 0.0, "trechos": []})
        d["km"] += r["km"]
        d["trechos"].append({"id": r["id"], "titulo": r["titulo"], "local": r["local"], "km": r["km"], "saida": r["saida"],
                             "chegada": r["chegada"], "min": minutos(r["saida"], r["chegada"]) if r["saida"] and r["chegada"] else None})
    return sorted(por.values(), key=lambda d: -d["km"])


@app.get("/gestor/equipe/historico", response_class=HTMLResponse)
def equipe_historico(request: Request, dias: int = 7):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    with db() as c:
        tecnicos = historico_rotas(c, u, dias if dias in (7, 30) else 7)
    return render(request, "_historico_rotas.html", tecnicos=tecnicos, dias=dias if dias in (7, 30) else 7)


@app.get("/gestor/equipe/painel", response_class=HTMLResponse)
def equipe_painel(request: Request):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    with db() as c:
        dados = painel_equipe(c, u)
        tipos = c.execute("SELECT id, setor_id FROM tipos ORDER BY id").fetchall()
    return render(request, "_equipe_painel.html", **dados, tipo_padrao=tipos[0]["id"] if tipos else 1)


@app.get("/gestor/equipe/dados")
def equipe_dados(request: Request):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    with db() as c:
        d = painel_equipe(c, u)
    return JSONResponse({
        "base": list(BASE_RASTRO),
        "tecnicos": [{"nome": t["nome"], "posicao": t["posicao"], "disponivel": t["disponivel"], "total": t["total"],
                      "atual": ({"id": t["atual"]["id"], "titulo": t["atual"]["titulo"], "local": t["atual"]["local"],
                                 "status": t["atual"]["status"]} if t["atual"] else None)} for t in d["tecnicos"]],
        "pendentes": [{"id": p["t"]["id"], "titulo": p["t"]["titulo"], "local": p["t"]["local"], "prio": p["prio"],
                       "alvo": p["alvo"]} for p in d["pendentes"]],
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- sino de notificações (todos os perfis)
def notificar(c, destinos, titulo, texto, url, excluir=None):
    """Grava um aviso para cada destino ('u:<id>' para usuários, 's:<e-mail>' para o solicitante)."""
    for d in {x for x in destinos if x and x != excluir}:
        c.execute("INSERT INTO notificacoes(dest,titulo,texto,url,criada_em) VALUES(?,?,?,?,?)",
                  (d, titulo[:120], (texto or "")[:240], url, agora()))


def _usuarios_com_papel(c, papel, area=None):
    if papel == "gestor_hd":
        sql, params = "SELECT id FROM usuarios WHERE papeis LIKE '%gestor%' AND papeis NOT LIKE '%gestor_tecnico%'", ()
    elif papel == "gestor_tecnico":
        sql, params = "SELECT id FROM usuarios WHERE papeis LIKE '%gestor_tecnico%' AND area_id=?", (area,)
    else:
        sql, params = "SELECT id FROM usuarios WHERE papeis LIKE ?", (f"%{papel}%",)
    return [f"u:{r['id']}" for r in c.execute(sql, params)]


def _notificar_status(c, t_antigo, novo, usuario, texto):
    t = c.execute("SELECT * FROM tarefas WHERE id=?", (t_antigo["id"],)).fetchone()
    ator = f"u:{usuario['id']}" if usuario else None
    rotulo = STATUS[novo][1]
    resumo = f"{t['titulo']} · {texto}" if texto else t["titulo"]
    if t["email"]:
        notificar(c, [f"s:{t['email'].lower()}"], f"Chamado #{t['id']}: {rotulo}", resumo, f"/validar/{t['token']}", ator)
    if t["executor_id"] and novo in ("encaminhado", "cancelado", "reaberto"):
        titulo = f"Nova missão #{t['id']}" if novo == "encaminhado" else f"Missão #{t['id']}: {rotulo}"
        notificar(c, [f"u:{t['executor_id']}"], titulo, resumo, f"/campo/{t['id']}", ator)
    if novo in ("devolvido", "impedido", "cancelado", "reaberto", "concluido"):
        notificar(c, _usuarios_com_papel(c, "gestor_hd"), f"Chamado #{t['id']}: {rotulo}", resumo, f"/tarefa/{t['id']}", ator)
    if novo in ("devolvido", "impedido", "reaberto") and t["setor_id"]:
        notificar(c, _usuarios_com_papel(c, "gestor_tecnico", t["setor_id"]), f"Chamado #{t['id']}: {rotulo}", resumo,
                  f"/tarefa/{t['id']}", ator)
    if novo == "reaberto":
        notificar(c, _usuarios_com_papel(c, "atendente"), f"Chamado #{t['id']} reaberto", resumo, f"/tarefa/{t['id']}", ator)


def dest_do_perfil(request, c, perfil):
    if perfil == "campo":
        u = usuario_do_cookie(request, "sess_campo")
        return f"u:{u['id']}" if u else None
    if perfil == "painel":
        u = usuario_do_cookie(request, "sess_painel")
        return f"u:{u['id']}" if u else None
    email = email_solicitante_atual(request, c)
    return f"s:{email}" if email else None


def _fragmento_sino(request, perfil):
    with db() as c:
        dest = dest_do_perfil(request, c, perfil)
        itens = c.execute("SELECT * FROM notificacoes WHERE dest=? ORDER BY id DESC LIMIT 12", (dest,)).fetchall() if dest else []
        nao_lidas = c.execute("SELECT count(*) n FROM notificacoes WHERE dest=? AND lida=0", (dest,)).fetchone()["n"] if dest else 0
    return render(request, "_sino.html", itens=itens, nao_lidas=nao_lidas, perfil=perfil, dest=dest)


@app.get("/notificacoes/sino", response_class=HTMLResponse)
def sino(request: Request, perfil: str = "publico"):
    return _fragmento_sino(request, perfil if perfil in ("campo", "painel", "publico") else "publico")


@app.post("/notificacoes/lidas", response_class=HTMLResponse)
def sino_marcar_lidas(request: Request, perfil: str = Form("publico")):
    with db() as c:
        dest = dest_do_perfil(request, c, perfil)
        if dest:
            c.execute("UPDATE notificacoes SET lida=1 WHERE dest=?", (dest,))
    return _fragmento_sino(request, perfil if perfil in ("campo", "painel", "publico") else "publico")


@app.get("/notificacoes/{nid}/ir")
def sino_abrir(request: Request, nid: int, perfil: str = "publico"):
    with db() as c:
        dest = dest_do_perfil(request, c, perfil)
        n = c.execute("SELECT * FROM notificacoes WHERE id=? AND dest=?", (nid, dest)).fetchone() if dest else None
        if not n:
            raise HTTPException(404, "Notificação não encontrada")
        c.execute("UPDATE notificacoes SET lida=1 WHERE id=?", (nid,))
    return redirect(n["url"] or "/")


@app.get("/notificacoes", response_class=HTMLResponse)
def notificacoes_pagina(request: Request, perfil: str = "", so_novas: int = 0):
    if perfil not in ("campo", "painel", "publico"):
        perfil = "campo" if request.cookies.get("sess_campo") and not request.cookies.get("sess_painel") else (
            "painel" if request.cookies.get("sess_painel") else "publico")
    with db() as c:
        dest = dest_do_perfil(request, c, perfil)
        if not dest:
            return redirect("/login?msg=Entre para ver suas notificações.")
        itens = c.execute("SELECT * FROM notificacoes WHERE dest=?" + (" AND lida=0" if so_novas else "") + " ORDER BY id DESC LIMIT 200",
                          (dest,)).fetchall()
        nao_lidas = c.execute("SELECT count(*) n FROM notificacoes WHERE dest=? AND lida=0", (dest,)).fetchone()["n"]
        u = usuario_do_cookie(request, "sess_campo" if perfil == "campo" else "sess_painel") if perfil != "publico" else None
    return render(request, "notificacoes.html", itens=itens, nao_lidas=nao_lidas, perfil=perfil, so_novas=so_novas, u=u)


@app.get("/gestor/metricas", response_class=HTMLResponse)
def gestor_metricas(request: Request, dias: int = 90, setor_id: int | None = None):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    if eh_gestor_tecnico(u):
        setor_id = u["area_id"]
    dias = dias if dias in PERIODOS else 90
    with db() as c:
        mt = metricas(c, dias, setor_id)
        setores = c.execute("SELECT * FROM setores").fetchall()
        analise = cfg(c, "analise_capacitacao")
    return render(request, "metricas.html", u=u, mt=mt, setores=setores, setor_id=setor_id, periodos=PERIODOS,
                  analise=analise)


@app.get("/gestor/metricas/analise", response_class=HTMLResponse)
def ver_analise(request: Request):
    exige_painel(request, "gestor")
    with db() as c:
        analise = cfg(c, "analise_capacitacao")
    return render(request, "_analise.html", analise=analise)


@app.post("/gestor/metricas/analise", response_class=HTMLResponse)
def pedir_analise(request: Request, dias: int = Form(90)):
    exige_painel(request, "gestor")
    dias = dias if dias in PERIODOS else 90
    with db() as c:
        anterior = cfg(c, "analise_capacitacao") or {}
        set_cfg(c, "analise_capacitacao", anterior | {"status": "gerando", "dias": dias})
        analise = cfg(c, "analise_capacitacao")
    em_segundo_plano(gerar_analise_capacitacao, dias)
    return render(request, "_analise.html", analise=analise)


@app.get("/tarefa/{tid}", response_class=HTMLResponse)
def tarefa_detalhe(request: Request, tid: int):
    u = exige_painel(request, "atendente", "gestor", "gestor_tecnico")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        exige_acesso(u, t)
        ctx = dict(
            t=t, u=u, eh_gestor="gestor" in u["papeis"],
            ia=json.loads(t["ia_json"]) if t["ia_json"] else None,
            tipos=[x for x in c.execute("SELECT t.*, s.nome setor FROM tipos t JOIN setores s ON s.id=t.setor_id").fetchall()
                   if not eh_gestor_tecnico(u) or x["setor_id"] == u["area_id"]],
            executores=carga_executores(c, u["area_id"] if eh_gestor_tecnico(u) else None),
            todos=c.execute("SELECT * FROM usuarios ORDER BY nome").fetchall(),
            eventos=c.execute("SELECT * FROM eventos WHERE tarefa_id=? ORDER BY id DESC", (tid,)).fetchall(),
            impedimentos=c.execute("SELECT i.*, u.nome apoio FROM impedimentos i LEFT JOIN usuarios u ON u.id=i.apoio_id "
                                   "WHERE tarefa_id=? ORDER BY id DESC", (tid,)).fetchall(),
            outbox=c.execute("SELECT * FROM outbox WHERE tarefa_id=? ORDER BY id DESC", (tid,)).fetchall(),
            executor=c.execute("SELECT * FROM usuarios WHERE id=?", (t["executor_id"],)).fetchone(),
            tipo=c.execute("SELECT * FROM tipos WHERE id=?", (t["tipo_id"],)).fetchone(),
            setor=c.execute("SELECT * FROM setores WHERE id=?", (t["setor_id"],)).fetchone(),
            pontos=c.execute("SELECT p.*, u.nome FROM pontos p JOIN usuarios u ON u.id=p.usuario_id WHERE tarefa_id=? "
                             "ORDER BY p.id", (tid,)).fetchall(),
            ultimo=c.execute("SELECT * FROM eventos WHERE tarefa_id=? AND para=? ORDER BY id DESC LIMIT 1",
                             (tid, t["status"])).fetchone(),
        )
    return render(request, "tarefa.html", **ctx)


@app.post("/tarefa/{tid}/cancelar")
def cancelar(request: Request, tid: int, motivo: str = Form(...)):
    """Atendente ou gestor cancela o chamado (duplicado, aberto por engano, solicitante desistiu…)."""
    u = exige_painel(request, "atendente", "gestor", "gestor_tecnico")
    if not motivo.strip():
        return redirect(f"/tarefa/{tid}?msg=Informe o motivo do cancelamento")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        exige_acesso(u, t)
        c.execute("UPDATE impedimentos SET aberto=0, providencia='Chamado cancelado', resolvido_por=?, resolvido_em=? "
                  "WHERE tarefa_id=? AND aberto=1", (u["nome"], agora(), tid))
        mudar_status(c, t, "cancelado", u, "gestor" if "gestor" in u["papeis"] else "atendente",
                     f"Chamado cancelado: {motivo.strip()}")
    if t["executor_id"] and t["status"] in ATIVOS:
        em_segundo_plano(enviar_push, t["executor_id"], f"Chamado cancelado – #{tid}", motivo.strip(), "/campo")
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{tid}?msg=Chamado cancelado")


@app.get("/tarefa/{tid}/estado")
def tarefa_estado(request: Request, tid: int, v: str = ""):
    u = exige_painel(request)
    with db() as c:
        exige_acesso(u, tarefa_ou_404(c, tid))
        return recarregar_se_mudou(c, tid, v)


@app.post("/tarefa/{tid}/atribuir")
def atribuir(request: Request, tid: int, executor_id: int = Form(...), tipo_id: int = Form(...),
             prioridade: str = Form(...), observacao: str = Form("")):
    """O atendimento (ou o gestor) avalia o chamado da fila e o encaminha ao time técnico."""
    u = exige_painel(request, "atendente", "gestor", "gestor_tecnico")
    papel = papel_painel(u)
    with db() as c:
        t = tarefa_ou_404(c, tid)
        exige_acesso(u, t)
        if t["status"] == "devolvido" and not eh_gestor_tecnico(u):
            raise HTTPException(403, "Chamado devolvido pelo técnico: quem reavalia é o gestor técnico da área.")
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        ia_status = t["ia_status"]
        if ia_status == "sugerida":
            iguais = (ia.get("executor_id") == executor_id and ia.get("tipo_id") == tipo_id
                      and ia.get("prioridade") == prioridade)
            ia_status = "aceita" if iguais else "editada"
        tipo = c.execute("SELECT * FROM tipos WHERE id=?", (tipo_id,)).fetchone()
        ex = c.execute("SELECT * FROM usuarios WHERE id=?", (executor_id,)).fetchone()
        if not ex or not ex["disponivel"]:
            raise HTTPException(409, "Este técnico está inativo e não recebe novas missões.")
        if eh_gestor_tecnico(u):
            area_ex = c.execute("SELECT setor_id FROM equipes WHERE id=?", (ex["equipe_id"],)).fetchone()
            if not area_ex or area_ex["setor_id"] != u["area_id"] or tipo["setor_id"] != u["area_id"]:
                raise HTTPException(403, "Direcione apenas a técnicos e tipos da sua área.")
        ajustada_por = t["prioridade_ajustada_por"]
        if prioridade != t["prioridade"] and ia.get("prioridade") and prioridade != ia["prioridade"]:
            ajustada_por = u["nome"]
        priorizou = (f"IA, confirmada por {u['nome']}" if ia.get("prioridade") == prioridade else u["nome"])
        c.execute("UPDATE tarefas SET executor_id=?, tipo_id=?, setor_id=?, prioridade=?, prioridade_ajustada_por=?, "
                  "ia_status=?, prioridade_por=? WHERE id=?",
                  (executor_id, tipo_id, tipo["setor_id"], prioridade, ajustada_por, ia_status, priorizou, tid))
        area = c.execute("SELECT nome FROM setores WHERE id=?", (tipo["setor_id"],)).fetchone()["nome"]
        texto = f"Encaminhado a {ex['nome']} · área {area} · {tipo['nome']} · {prioridade} ({PRIORIDADES[prioridade]})"
        if ia_status in ("aceita", "editada") and t["ia_status"] == "sugerida":
            texto += f" · sugestão da IA {'aceita' if ia_status == 'aceita' else 'conferida e editada'} pelo {papel}"
        if observacao:
            texto += f" · Obs.: {observacao}"
        mudar_status(c, t, "encaminhado", u, papel, texto)
    em_segundo_plano(enviar_push, executor_id, f"Nova missão {prioridade} – #{tid}",
                     f"{t['titulo']} · {t['local']}", f"/campo/{tid}")
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{tid}?msg=Chamado encaminhado a {ex['nome']} – notificação push enviada")


@app.post("/tarefa/{tid}/resolver-atendimento")
def resolver_no_atendimento(request: Request, tid: int, solucao: str = Form(...)):
    """1º nível: o atendente resolve sem acionar o time técnico; a demandante confirma pelo link, como nos demais."""
    u = exige_painel(request, "atendente", "gestor")
    if not solucao.strip():
        return redirect(f"/tarefa/{tid}?msg=Descreva a solução dada ao solicitante")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        exige_acesso(u, t)
        if t["status"] not in A_ENCAMINHAR:
            raise HTTPException(409, "Só chamados na fila do atendimento podem ser resolvidos no 1º nível")
        c.execute("UPDATE tarefas SET relato=?, resolvido_atendimento_por=? WHERE id=?", (solucao.strip(), u["nome"], tid))
        mudar_status(c, t, "executado", u, papel_painel(u), f"Resolvido no atendimento (1º nível): {solucao.strip()}")
    em_segundo_plano(resumir_conclusao, tid)
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{tid}?msg=Chamado resolvido no atendimento – aguardando confirmação do solicitante")


@app.post("/tarefa/{tid}/prioridade")
def ajustar_prioridade(request: Request, tid: int, prioridade: str = Form(...), motivo: str = Form(...)):
    """Atendente ou gestor corrige a criticidade manualmente quando discorda da IA (fica registrado na trilha)."""
    u = exige_painel(request, "atendente", "gestor", "gestor_tecnico")
    if prioridade not in PRIORIDADES:
        raise HTTPException(422, "Criticidade inválida")
    if not motivo.strip():
        return redirect(f"/tarefa/{tid}?msg=Informe o motivo do ajuste de criticidade")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        exige_acesso(u, t)
        if t["status"] in FINAIS:
            raise HTTPException(409, f"Chamado {STATUS[t['status']][1].lower()}: a criticidade não pode mais ser alterada")
        if prioridade == t["prioridade"]:
            return redirect(f"/tarefa/{tid}?msg=A criticidade já é {prioridade}")
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        papel = "gestor" if "gestor" in u["papeis"] else "atendente"
        c.execute("UPDATE tarefas SET prioridade=?, prioridade_ajustada_por=?, prioridade_por=?, atualizado_em=? WHERE id=?",
                  (prioridade, u["nome"], u["nome"], agora(), tid))
        texto = (f"Criticidade ajustada manualmente: {t['prioridade'] or 'sem prioridade'} → {prioridade} "
                 f"({PRIORIDADES[prioridade]})")
        if ia.get("prioridade"):
            texto += f" · IA havia sugerido {ia['prioridade']}"
        registrar_evento(c, tid, u["nome"], papel, f"{texto} · Motivo: {motivo.strip()}")
    if t["executor_id"] and t["status"] in ATIVOS:
        em_segundo_plano(enviar_push, t["executor_id"], f"Criticidade alterada – #{tid}",
                         f"{t['titulo']}: agora {prioridade} ({PRIORIDADES[prioridade]})", f"/campo/{tid}")
    return redirect(f"/tarefa/{tid}?msg=Criticidade alterada para {prioridade} – {PRIORIDADES[prioridade]}")


@app.post("/tarefa/{tid}/rejeitar-ia")
def rejeitar_ia(request: Request, tid: int):
    u = exige_painel(request, "atendente", "gestor", "gestor_tecnico")
    with db() as c:
        exige_acesso(u, tarefa_ou_404(c, tid))
        c.execute("UPDATE tarefas SET ia_status='rejeitada', atualizado_em=? WHERE id=?", (agora(), tid))
        registrar_evento(c, tid, u["nome"], papel_painel(u), f"Sugestão da IA rejeitada pelo {papel_painel(u)}")
    return redirect(f"/tarefa/{tid}")


@app.post("/tarefa/{tid}/retriar")
def retriar(request: Request, tid: int):
    u = exige_painel(request, "gestor", "atendente", "gestor_tecnico")
    with db() as c:
        exige_acesso(u, tarefa_ou_404(c, tid))
        c.execute("UPDATE tarefas SET ia_status='analisando', atualizado_em=? WHERE id=?", (agora(), tid))
    em_segundo_plano(triar, tid)
    return redirect(f"/tarefa/{tid}")


@app.post("/impedimento/{iid}/resolver")
def resolver_impedimento(request: Request, iid: int, providencia: str = Form(...), apoio_id: int | None = Form(None)):
    u = exige_painel(request, "gestor", "gestor_tecnico")
    with db() as c:
        i = c.execute("SELECT * FROM impedimentos WHERE id=? AND aberto=1", (iid,)).fetchone()
        if i:
            exige_acesso(u, tarefa_ou_404(c, i["tarefa_id"]))
        if not i:
            raise HTTPException(404, "Impedimento não encontrado ou já resolvido")
        t = tarefa_ou_404(c, i["tarefa_id"])
        c.execute("UPDATE impedimentos SET aberto=0, providencia=?, apoio_id=?, resolvido_por=?, resolvido_em=? WHERE id=?",
                  (providencia, apoio_id, u["nome"], agora(), iid))
        rg = regras(c)
        lancar(c, u["id"], t["id"], rg["bonus_desbloqueio_gestor"], "bonus",
               f"Desbloqueio da missão #{t['id']} ({i['motivo']})")
        apoio = c.execute("SELECT * FROM usuarios WHERE id=?", (apoio_id,)).fetchone() if apoio_id else None
        if apoio and apoio["id"] != t["executor_id"]:
            lancar(c, apoio["id"], t["id"], rg["bonus_apoio_colega"], "bonus", f"Apoio à missão #{t['id']} ({i['motivo']})")
        mudar_status(c, t, "em_execucao", u, "gestor",
                     f"Impedimento resolvido: {providencia}" + (f" (apoio: {apoio['nome']})" if apoio else ""))
    em_segundo_plano(enviar_push, t["executor_id"], f"Impedimento resolvido – #{t['id']}", providencia, f"/campo/{t['id']}")
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{t['id']}?msg=Impedimento resolvido – executor notificado")


# ---------------------------------------------------------------- executor (web mobile, Req. 3)

@app.get("/campo", response_class=HTMLResponse)
def campo(request: Request):
    u = exige_executor(request)
    return render(request, "campo.html", u=u)


@app.post("/campo/disponibilidade")
def campo_disponibilidade(request: Request, ativo: int = Form(...)):
    """O técnico escolhe se recebe novas missões. Missões em andamento continuam com ele."""
    u = exige_executor(request)
    with db() as c:
        c.execute("UPDATE usuarios SET disponivel=? WHERE id=?", (1 if ativo else 0, u["id"]))
    return redirect("/campo?msg=" + ("Você está ativo e pode receber novas missões" if ativo else "Você está inativo: não receberá novas missões"))


@app.get("/campo/lista", response_class=HTMLResponse)
def campo_lista(request: Request):
    u = exige_executor(request)
    with db() as c:
        tarefas = c.execute("SELECT * FROM tarefas WHERE executor_id=? AND status IN "
                            "('encaminhado','a_caminho','em_execucao','impedido','executado','reaberto') "
                            "ORDER BY CASE status WHEN 'encaminhado' THEN 0 ELSE 1 END, coalesce(prioridade,'P9'), id DESC",
                            (u["id"],)).fetchall()
        feitas = c.execute("SELECT * FROM tarefas WHERE executor_id=? AND status='concluido' ORDER BY id DESC LIMIT 5",
                           (u["id"],)).fetchall()
        temp = temporada_atual(c)
        pts = c.execute("SELECT coalesce(sum(CASE WHEN tipo IN ('definitivo','bonus') THEN pontos END),0) d, "
                        "coalesce(sum(CASE WHEN tipo='provisorio' THEN pontos END),0) p FROM pontos "
                        "WHERE usuario_id=? AND temporada=?", (u["id"], temp)).fetchone()
        total = c.execute("SELECT coalesce(sum(pontos),0) n FROM pontos WHERE usuario_id=? AND tipo IN ('definitivo','bonus')",
                          (u["id"],)).fetchone()["n"]
        meds = medalhas(c, u["id"])
    return render(request, "_campo_lista.html", u=u, tarefas=tarefas, feitas=feitas, pts=pts, nivel=nivel(total),
                  medalhas=meds, ultima=max([t["id"] for t in tarefas if t["status"] == "encaminhado"], default=0))


# ---------------------------------------------------------------- rota otimizada do técnico
def _log_rota(c, executor_id, texto):
    c.execute("INSERT INTO rota_log(executor_id,texto,criado_em) VALUES(?,?,?)", (executor_id, texto, agora()))


def rota_do_tecnico(c, u):
    ativas = c.execute("SELECT * FROM tarefas WHERE executor_id=? AND status IN ('encaminhado','a_caminho','em_execucao') ORDER BY id",
                       (u["id"],)).fetchall()
    origem, atual = _posicao_do_tecnico(c, ativas)
    paradas = [{"id": t["id"], "ponto": rastro_destino(chave_local(t)), "prio": t["prioridade"], "titulo": t["titulo"],
                "local": t["local"], "status": t["status"], "por": t["prioridade_por"] or t["prioridade_ajustada_por"]}
               for t in ativas if t["status"] in ("encaminhado", "a_caminho")]
    pref = c.execute("SELECT criterio FROM rota_prefs WHERE executor_id=?", (u["id"],)).fetchone()
    criterio = pref["criterio"] if pref and pref["criterio"] in CRITERIOS else "misto"
    salvo = {r["tarefa_id"]: r["pos"] for r in c.execute("SELECT tarefa_id, pos FROM rota_ordem WHERE executor_id=?", (u["id"],))}
    if salvo and any(p["id"] in salvo for p in paradas):
        # ordem manual do técnico; paradas novas entram no fim, pelo critério escolhido
        fixas = sorted((p for p in paradas if p["id"] in salvo), key=lambda p: salvo[p["id"]])
        novas = [p for p in ordenar_rota(origem, [p for p in paradas if p["id"] not in salvo], criterio)]
        ordem = calcular_trechos(origem, fixas + [{k: v for k, v in p.items() if k not in ("trecho_km", "acumulado_km", "eta_min")} for p in novas])
        manual = True
    else:
        ordem, manual = ordenar_rota(origem, paradas, criterio), False
    pontos, ant = [list(origem)], tuple(origem)
    for p in ordem:
        pontos += caminho_rua(ant, p["ponto"])[1:]
        ant = tuple(p["ponto"])
    historico = c.execute("SELECT * FROM rota_log WHERE executor_id=? ORDER BY id DESC LIMIT 15", (u["id"],)).fetchall()
    return {"origem": list(origem), "atual": ({"id": atual["id"], "titulo": atual["titulo"], "local": atual["local"]} if atual else None),
            "paradas": ordem, "caminho": pontos, "criterio": criterio, "criterios": CRITERIOS, "manual": manual,
            "historico": [dict(h) for h in historico],
            "total_km": round(ordem[-1]["acumulado_km"], 1) if ordem else 0,
            "total_min": ordem[-1]["eta_min"] if ordem else 0}


@app.post("/campo/rota/mover")
def campo_rota_mover(request: Request, tarefa_id: int = Form(...), direcao: str = Form(...)):
    u = exige_executor(request)
    if direcao not in ("cima", "baixo"):
        raise HTTPException(422, "Direção inválida")
    with db() as c:
        rota = rota_do_tecnico(c, u)
        ids = [p["id"] for p in rota["paradas"]]
        if tarefa_id not in ids:
            raise HTTPException(404, "Parada não encontrada")
        i = ids.index(tarefa_id)
        j = i - 1 if direcao == "cima" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            c.execute("DELETE FROM rota_ordem WHERE executor_id=?", (u["id"],))
            c.executemany("INSERT INTO rota_ordem(executor_id,tarefa_id,pos) VALUES(?,?,?)", [(u["id"], t, n) for n, t in enumerate(ids)])
            _log_rota(c, u["id"], f"Parada #{tarefa_id} movida para a posição {j + 1} (ordem manual)")
    return redirect("/campo/rota")


@app.post("/campo/rota/reotimizar")
def campo_rota_reotimizar(request: Request, criterio: str = Form("misto")):
    u = exige_executor(request)
    if criterio not in CRITERIOS:
        raise HTTPException(422, "Critério inválido")
    with db() as c:
        c.execute("DELETE FROM rota_ordem WHERE executor_id=?", (u["id"],))
        c.execute("INSERT INTO rota_prefs(executor_id,criterio) VALUES(?,?) ON CONFLICT(executor_id) DO UPDATE SET criterio=excluded.criterio",
                  (u["id"], criterio))
        rota = rota_do_tecnico(c, u)
        _log_rota(c, u["id"], f"Rota reotimizada pelo critério \"{CRITERIOS[criterio]}\": {len(rota['paradas'])} paradas, {rota['total_km']} km")
    return redirect("/campo/rota")


@app.get("/campo/rota", response_class=HTMLResponse)
def campo_rota(request: Request):
    u = exige_executor(request)
    with db() as c:
        rota = rota_do_tecnico(c, u)
    return render(request, "rota.html", u=u, rota=rota)


@app.get("/campo/rota/dados")
def campo_rota_dados(request: Request):
    u = exige_executor(request)
    with db() as c:
        rota = rota_do_tecnico(c, u)
    return JSONResponse({**rota, "paradas": [{**p, "ponto": list(p["ponto"])} for p in rota["paradas"]]},
                        headers={"Cache-Control": "no-store"})


@app.get("/campo/{tid}", response_class=HTMLResponse)
def campo_missao(request: Request, tid: int):
    u = exige_executor(request)
    with db() as c:
        t = tarefa_ou_404(c, tid)
        if t["executor_id"] != u["id"]:
            raise HTTPException(403, "Esta missão não está atribuída a você")
        ctx = dict(
            t=t, u=u, ia=json.loads(t["ia_json"]) if t["ia_json"] else None,
            tipo=c.execute("SELECT * FROM tipos WHERE id=?", (t["tipo_id"],)).fetchone(),
            impedimento=c.execute("SELECT * FROM impedimentos WHERE tarefa_id=? AND aberto=1", (tid,)).fetchone(),
            chat=c.execute("SELECT * FROM chat WHERE tarefa_id=? ORDER BY id", (tid,)).fetchall(),
            pontos=c.execute("SELECT * FROM pontos WHERE tarefa_id=? AND usuario_id=? ORDER BY id", (tid, u["id"])).fetchall(),
            eventos=c.execute("SELECT * FROM eventos WHERE tarefa_id=? AND papel<>'ia' ORDER BY id DESC LIMIT 8", (tid,)).fetchall(),
        )
    return render(request, "missao.html", **ctx)


@app.get("/campo/{tid}/estado")
def campo_estado(request: Request, tid: int, v: str = ""):
    exige_executor(request)
    with db() as c:
        return recarregar_se_mudou(c, tid, v)


def _missao_do_executor(c, request, tid):
    u = exige_executor(request)
    t = tarefa_ou_404(c, tid)
    if t["executor_id"] != u["id"]:
        raise HTTPException(403, "Esta missão não está atribuída a você")
    return u, t


async def salvar_foto(foto: UploadFile | None, prefixo):
    if not foto or not foto.filename:
        return None
    ext = Path(foto.filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"):
        raise HTTPException(422, "Envie uma imagem (jpg, png, webp)")
    nome = f"{prefixo}_{secrets.token_hex(6)}{ext}"
    (UPLOADS / nome).write_bytes(await foto.read())
    return nome


@app.post("/campo/{tid}/avancar")
def campo_avancar(request: Request, tid: int, para: str = Form(...), observacao: str = Form("")):
    with db() as c:
        u, t = _missao_do_executor(c, request, tid)
        if para not in ("a_caminho", "em_execucao") or t["status"] == "impedido":
            raise HTTPException(409, "Ação não permitida neste estado")
        textos = {"a_caminho": "Missão aceita – executor a caminho do local",
                  "em_execucao": "Executor chegou ao local e iniciou o atendimento"}
        mudar_status(c, t, para, u, "executor", textos[para] + (f" · {observacao}" if observacao else ""))
        if para == "em_execucao":
            c.execute("UPDATE tarefas SET km_percorrido=? WHERE id=?", (distancia_km(chave_local(t)), tid))
    em_segundo_plano(processar_outbox)
    msg = "Atendimento iniciado no local" if para == "em_execucao" else "Status atualizado: a caminho"
    return redirect(f"/campo/{tid}?msg={msg}")


@app.post("/campo/{tid}/devolver")
def campo_devolver(request: Request, tid: int, motivo: str = Form(...), detalhe: str = Form("")):
    """O técnico devolve o chamado à triagem: falta informação, é de outra área ou não é problema técnico."""
    if motivo not in MOTIVOS_DEVOLUCAO:
        raise HTTPException(422, "Motivo inválido")
    if motivo == "Outro" and not detalhe.strip():
        return redirect(f"/campo/{tid}?msg=Explique o motivo da devolução")
    with db() as c:
        u, t = _missao_do_executor(c, request, tid)
        mudar_status(c, t, "devolvido", u, "executor", f"Devolvido ao gestor técnico da área: {motivo}" + (f" – {detalhe}" if detalhe else ""))
    em_segundo_plano(processar_outbox)
    return redirect("/campo?msg=Chamado devolvido ao gestor técnico da sua área, que vai reavaliar.")


@app.post("/campo/{tid}/impedimento")
async def campo_impedimento(request: Request, tid: int, motivo: str = Form(...), detalhe: str = Form(""),
                            foto: UploadFile | None = File(None)):
    if motivo not in MOTIVOS_IMPEDIMENTO:
        raise HTTPException(422, "Motivo inválido")
    arquivo = await salvar_foto(foto, f"t{tid}_imp")
    with db() as c:
        u, t = _missao_do_executor(c, request, tid)
        externo = int(motivo in MOTIVOS_EXTERNOS)
        cur = c.execute("INSERT INTO impedimentos(tarefa_id,motivo,detalhe,externo,criado_em) VALUES(?,?,?,?,?)",
                        (tid, motivo, detalhe, externo, agora()))
        mudar_status(c, t, "impedido", u, "executor", f"Impedimento: {motivo}" + (f" – {detalhe}" if detalhe else ""))
        if arquivo:
            registrar_evento(c, tid, u["nome"], "executor", "Foto do impedimento", anexo=arquivo)
    em_segundo_plano(sugerir_apoio, cur.lastrowid)
    em_segundo_plano(processar_outbox)
    aviso = " O relógio do SLA está pausado." if externo else ""
    return redirect(f"/campo/{tid}?msg=Impedimento registrado – gestor avisado.{aviso}")


@app.post("/campo/{tid}/concluir")
async def campo_concluir(request: Request, tid: int, relato: str = Form(...), foto: UploadFile | None = File(None)):
    arquivo = await salvar_foto(foto, f"t{tid}_evid")
    with db() as c:
        u, t = _missao_do_executor(c, request, tid)
        c.execute("UPDATE tarefas SET relato=?, evidencia=coalesce(?,evidencia) WHERE id=?", (relato, arquivo, tid))
        mudar_status(c, t, "executado", u, "executor", f"Execução informada pelo técnico: {relato}")
        if arquivo:
            registrar_evento(c, tid, u["nome"], "executor", "Evidência anexada", anexo=arquivo)
        pts = pontuar_conclusao(c, tarefa_ou_404(c, tid))
    em_segundo_plano(resumir_conclusao, tid)
    em_segundo_plano(processar_outbox)
    return redirect(f"/campo/{tid}?msg=Chamado executado! +{pts} pts provisórios – aguardando confirmação da demandante")


@app.post("/campo/{tid}/chat", response_class=HTMLResponse)
def campo_chat(request: Request, tid: int, pergunta: str = Form(...)):
    with db() as c:
        _u, t = _missao_do_executor(c, request, tid)
        c.execute("INSERT INTO chat(tarefa_id,autor,texto,criado_em) VALUES(?,?,?,?)", (tid, "executor", pergunta, agora()))
    with db() as c:
        resposta, fonte = responder_chat(c, t, pergunta)
        c.execute("INSERT INTO chat(tarefa_id,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
                  (tid, "copiloto", resposta, fonte, agora()))
        chat = c.execute("SELECT * FROM chat WHERE tarefa_id=? ORDER BY id", (tid,)).fetchall()
    return render(request, "_chat.html", chat=chat)


# ---------------------------------------------------------------- push

@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    """Permite instalar o app na tela inicial: no iPhone, é o que habilita o push."""
    return JSONResponse({
        "name": "Help Desk SJP", "short_name": "Help Desk", "lang": "pt-BR", "start_url": "/login", "scope": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#1e4560",
        "icons": [
            {"src": "/static/ui/pwa/icone-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/ui/pwa/icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/ui/pwa/icone-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/api/push/chave", tags=["push"])
def push_chave():
    return {"chave": chave_publica_vapid()}


@app.post("/api/push/inscrever", tags=["push"])
def push_inscrever(request: Request, sub: dict):
    u = exige_executor(request)
    with db() as c:
        c.execute("INSERT INTO push_subs(usuario_id,endpoint,sub_json) VALUES(?,?,?) ON CONFLICT(endpoint) "
                  "DO UPDATE SET usuario_id=excluded.usuario_id, sub_json=excluded.sub_json",
                  (u["id"], sub["endpoint"], json.dumps(sub)))
    return {"ok": True}


@app.post("/api/push/teste", tags=["push"])
def push_teste(request: Request):
    u = exige_executor(request)
    em_segundo_plano(enviar_push, u["id"], "Notificações ativas", "Você receberá suas novas missões aqui.", "/campo")
    return {"ok": True}


# ---------------------------------------------------------------- demandante (Req. 4)

@app.get("/validar/{token}", response_class=HTMLResponse)
def validar_form(request: Request, token: str):
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE token=?", (token,)).fetchone()
        if not t:
            raise HTTPException(404, "Link inválido ou expirado")
        executor = c.execute("SELECT nome, avatar FROM usuarios WHERE id=?", (t["executor_id"],)).fetchone()
        linha = c.execute("SELECT para, criado_em FROM eventos WHERE tarefa_id=? AND para IS NOT NULL ORDER BY id",
                          (t["id"],)).fetchall()
        minhas = solicitacoes_do_visitante(request, c)
    return render(request, "validar.html", t=t, executor=executor, linha=linha, minhas=minhas)


@app.post("/validar/{token}")
def validar(request: Request, token: str, resultado: str = Form(...), nota: int = Form(...), comentario: str = Form("")):
    if resultado not in ("resolvido", "pendente") or not 1 <= nota <= 5:
        raise HTTPException(422, "Resposta inválida")
    if resultado == "pendente" and not comentario.strip():
        return redirect(f"/validar/{token}?msg=Conte o que ainda não está funcionando para reabrirmos o atendimento.")
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE token=?", (token,)).fetchone()
        if not t or t["status"] != "executado":
            raise HTTPException(409, "Esta tarefa não está aguardando sua confirmação")
        c.execute("UPDATE tarefas SET nota=?, comentario_demandante=? WHERE id=?", (nota, comentario, t["id"]))
        if resultado == "resolvido":
            mudar_status(c, t, "concluido", None, "demandante",
                         f"Resolução confirmada pela demandante ({nota}★)" + (f": {comentario}" if comentario else ""))
            if t["executor_id"] and not t["resolvido_atendimento_por"]:
                pontuar_validacao(c, tarefa_ou_404(c, t["id"]), nota)
            msg = "Obrigado! Sua confirmação foi registrada."
        else:
            c.execute("UPDATE tarefas SET reaberturas=reaberturas+1 WHERE id=?", (t["id"],))
            c.execute("UPDATE pontos SET tipo='suspenso' WHERE tarefa_id=? AND tipo='provisorio'", (t["id"],))
            mudar_status(c, t, "reaberto", None, "demandante", f"Pendência indicada pela demandante ({nota}★): {comentario}")
            msg = "Pendência registrada. O atendimento foi reaberto e a equipe vai retomar."
    em_segundo_plano(processar_outbox)
    return redirect(f"/validar/{token}?msg={msg}")


# ---------------------------------------------------------------- servidor: autoatendimento com IA (web)
# O servidor conversa com a IA antes de ligar para o Help Desk. Se não resolver, envia uma SOLICITAÇÃO, que um
# atendente confere e registra como tarefa (Req. 1: a abertura é feita por atendente ou responsável autorizado).

def contar_auto_resolvidos(c, inicio="", somente_ia=False):
    """Confirmação do solicitante, sem solicitação/OS; uma contagem por conversa."""
    sql = "SELECT count(*) FROM autoatendimento a WHERE a.resolvido=1 AND a.solicitacao_id IS NULL AND a.criado_em>=?"
    if somente_ia:
        sql += " AND EXISTS (SELECT 1 FROM auto_msgs m WHERE m.token=a.token AND m.autor='assistente' AND m.fonte IN ('llm','jev'))"
        sql += " AND NOT EXISTS (SELECT 1 FROM auto_msgs m WHERE m.token=a.token AND m.autor='assistente' AND coalesce(m.fonte,'') NOT IN ('llm','jev'))"
        sql += " AND NOT EXISTS (SELECT 1 FROM auto_msgs m WHERE m.token=a.token AND m.autor IN ('atendente','sistema'))"
    return c.execute(sql, (inicio,)).fetchone()[0]


def estado_conversa(token, conversa, resolvida=False):
    """Snapshot assinado permite retomar o chat entre instâncias do protótipo."""
    payload = json.dumps({"token": token, "resolvida": resolvida,
                          "expires": (datetime.now() + timedelta(days=1)).timestamp(),
                          "msgs": [{k: m[k] for k in ("autor", "texto", "fonte", "criado_em")} for m in conversa]},
                         ensure_ascii=False, separators=(",", ":")).encode()
    data = base64.urlsafe_b64encode(zlib.compress(payload)).decode()
    signature = hmac.new(SECRET.encode(), ("chat:" + data).encode(), hashlib.sha256).hexdigest()
    return data + "." + signature


def ler_estado_conversa(estado):
    if not estado or len(estado) > 100000:
        return None
    try:
        data, signature = estado.rsplit(".", 1)
        expected = hmac.new(SECRET.encode(), ("chat:" + data).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.urlsafe_b64decode(data), 200001)
        if len(raw) > 200000 or not decoder.eof:
            return None
        snapshot = json.loads(raw)
        return snapshot
    except (ValueError, KeyError, TypeError, zlib.error):
        return None


def restaurar_conversa(c, estado, token_cookie):
    snapshot = ler_estado_conversa(estado)
    if not snapshot or snapshot.get("resolvida"):
        return None
    try:
        token = snapshot["token"]
        if snapshot["expires"] < datetime.now().timestamp() or (token_cookie and token_cookie != token):
            return None
        row = c.execute("SELECT resolvido, solicitacao_id FROM autoatendimento WHERE token=?", (token,)).fetchone()
        if row and row["resolvido"]:
            return None
        c.execute("INSERT OR IGNORE INTO autoatendimento(token,criado_em) VALUES(?,?)", (token, agora()))
        count = c.execute("SELECT count(*) FROM auto_msgs WHERE token=?", (token,)).fetchone()[0]
        for m in snapshot["msgs"][count:]:
            c.execute("INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
                      (token, m["autor"], m["texto"], m["fonte"], m["criado_em"]))
        return token
    except (ValueError, KeyError, TypeError, zlib.error):
        return None


def _sessao_auto(request, c, estado=None):
    token = request.cookies.get("auto_token")
    restored = restaurar_conversa(c, estado or request.cookies.get("auto_estado"), token)
    if restored:
        return restored
    if token and c.execute("SELECT 1 FROM autoatendimento WHERE token=? AND resolvido=0",
                           (token,)).fetchone():
        return token
    return None


def solicitacoes_do_visitante(request, c):
    """Lista lateral do assistente: só aparece para quem já acessou 'Meus chamados' neste navegador (leitura apenas)."""
    recuperar_acesso_demo(request, c)
    token = request.cookies.get("acesso_solicitante")
    acesso = c.execute("SELECT email FROM acessos_solicitante WHERE token=? AND criado_em>=?", (
        token, (datetime.now() - timedelta(days=ACESSO_DIAS)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone() if token else None
    if not acesso:
        return []
    return c.execute("SELECT protocolo, titulo, criado_em, token FROM solicitacoes WHERE lower(email)=? "
                     "ORDER BY criado_em DESC LIMIT 12", (acesso["email"],)).fetchall()


FORA_ESCOPO = "Posso ajudar apenas com suporte do Helpdesk municipal, como computadores, sistemas, internet, impressoras e telefonia. Não posso ajudar com esse assunto."


def pedido_fora_escopo(pergunta):
    texto = pergunta.casefold()
    return bool(re.search(r"\breceita\s+(?:de|para|do|da)\s+(?:um\s+|uma\s+)?(?:bolo|pão|pao|torta|brigadeiro|comida)|\b(?:cozinhar|horóscopo|horoscopo|piada|poema|aposta|futebol)\b", texto))


def email_solicitante_atual(request, c):
    recuperar_acesso_demo(request, c)
    r = c.execute("SELECT email FROM acessos_solicitante WHERE token=? AND criado_em>=?",
                  (request.cookies.get('acesso_solicitante'), (datetime.now() - timedelta(days=ACESSO_DIAS)).strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    return r['email'] if r else None


def exige_solicitante(request):
    """O assistente e os chamados do solicitante exigem login: não há acesso como visitante."""
    with db() as c:
        email = email_solicitante_atual(request, c)
    if not email:
        raise HTTPException(303, headers={"Location": "/login?msg=" + quote("Entre com o seu perfil para usar o assistente e acompanhar chamados.")})
    return email


def chamado_da_conversa(c, token):
    return c.execute("SELECT s.*, t.status tarefa_status FROM autoatendimento a JOIN solicitacoes s ON s.id=a.solicitacao_id "
                     "LEFT JOIN tarefas t ON t.id=s.tarefa_id WHERE a.token=?", (token,)).fetchone() if token else None


def texto_status_chat(s):
    if s['tarefa_status']:
        status = STATUS[s['tarefa_status']][1]
    else:
        status = 'Aguardando conferência do Helpdesk' if s['status'] == 'aguardando' else 'Solicitação devolvida: ' + (s['motivo_descarte'] or s['status'])
    return f"{s['protocolo']}: {status}."


def responder_autoatendimento(c, token, pergunta):
    if pedido_fora_escopo(pergunta):
        return FORA_ESCOPO, 'fora_escopo'
    chamado = chamado_da_conversa(c, token)
    if chamado:
        return texto_status_chat(chamado) + ' Você pode continuar consultando o andamento nesta conversa.', 'sistema'
    historico = c.execute("SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id DESC LIMIT 10", (token,)).fetchall()
    relatos = [h["texto"] for h in reversed(historico) if h["autor"] == "servidor"]
    if not relatos or relatos[-1] != pergunta:
        relatos.append(pergunta)
    try:
        decisao = route_conversation(relatos, BASE_AUTOATENDIMENTO)
        c.execute("UPDATE autoatendimento SET jev_json=? WHERE token=?",
                  (json.dumps(decisao, ensure_ascii=False), token))
        acao = decisao["answers"]["acao"]["choice"]
        if acao == 'fora_escopo':
            return FORA_ESCOPO, 'fora_escopo'
        if acao == "esclarecer":
            return "Pode descrever o que acontece e qual mensagem de erro aparece? Se preferir, clique em 'Abrir solicitação'.", "jev"
        if acao == "helpdesk":
            return "Este caso precisa ser avaliado pelo Helpdesk. Clique em 'Abrir solicitação' e revise os dados; o atendente receberá o histórico e formalizará o chamado, se necessário.", "jev"
        if acao == "risco":
            return "Afaste-se do risco e não toque em equipamentos ou instalações. Acione o atendimento de emergência adequado se houver perigo imediato. Para registrar a demanda no Helpdesk, clique em 'Abrir solicitação'.", "jev"
    except JevError as exc:
        log.warning("avaliação de entrada indisponível: %s", exc)
        c.execute("UPDATE autoatendimento SET jev_json=NULL WHERE token=?", (token,))
    msgs = [{"role": "system", "content":
             "Você é o Assistente Virtual do Help Desk da Prefeitura de São José dos Pinhais, atendendo servidores "
             "municipais que NÃO são técnicos. Objetivo: resolver problemas simples com orientações seguras antes que "
             "precisem de atendimento. Recuse assuntos alheios ao suporte municipal, como receitas, entretenimento e pedidos pessoais; nessa recusa nunca sugira abrir chamado. "
             "precisem abrir um chamado. Regras: linguagem simples e cordial; no máximo 4 passos curtos por resposta; "
             "faça no máximo 1 pergunta por vez para entender o problema (o que aconteceu, onde, desde quando, afeta "
             "outras pessoas?); nunca peça senhas; nunca oriente abrir equipamentos, tomadas ou quadros elétricos; em "
             "risco à segurança (fogo, choque, vazamento forte) oriente afastar as pessoas e abrir a solicitação na hora. "
             "Se o problema foi resolvido, peça que clique em 'Resolvido'. Se não for possível resolver sozinho, diga "
             "para clicar em 'Abrir solicitação' — o atendente receberá o resumo desta conversa. Responda em português, "
             "em texto simples, sem markdown.\n"
             f"ORIENTAÇÕES PERMITIDAS: {json.dumps(BASE_AUTOATENDIMENTO, ensure_ascii=False)}"}]
    # A rota já inseriu a mensagem atual; não enviar a mesma pergunta duas vezes.
    anteriores = list(reversed(historico))
    if anteriores and anteriores[-1]["autor"] == "servidor" and anteriores[-1]["texto"] == pergunta:
        anteriores.pop()
    for h in anteriores:
        msgs.append({"role": "user" if h["autor"] == "servidor" else "assistant", "content": h["texto"]})
    msgs.append({"role": "user", "content": pergunta})
    try:
        resposta = llm(msgs, max_tokens=800, effort=os.getenv("HAIKU_CHAT_EFFORT", "low"))
        return re.sub(r"\*\*|^#+\s*", "", resposta, flags=re.M), "llm"
    except Exception as e:  # noqa: BLE001
        log.warning("autoatendimento LLM falhou (%s)", e)
        texto = " ".join([pergunta] + [h["texto"] for h in historico if h["autor"] == "servidor"]).lower()
        for tp in c.execute("SELECT palavras, base_conhecimento FROM tipos WHERE base_conhecimento<>''"):
            if any(p.strip() and p.strip() in texto for p in tp["palavras"].split(",")):
                return (f"{BASE_AUTOATENDIMENTO[tp['base_conhecimento']]}\nSe não resolver, clique em 'Abrir solicitação'.",
                        "regras")
        return ("Ainda não consegui identificar o problema. Pode descrever o que acontece, onde e desde quando? "
                "Se preferir, clique em 'Abrir solicitação'.", "regras")


def rascunho_da_conversa(c, token):
    """IA transforma a conversa em uma solicitação estruturada (o servidor revisa antes de enviar)."""
    conversa = c.execute("SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
    falas = [m["texto"] for m in conversa if m["autor"] == "servidor"]
    setores = [dict(s) for s in c.execute("SELECT id, nome FROM setores")]
    try:
        r = extrair_json(llm([{"role": "user", "content":
            "Transforme a conversa abaixo em uma solicitação de atendimento. Responda SOMENTE JSON com as chaves: "
            '"titulo" (até 70 caracteres), "descricao" (2-3 frases objetivas do problema), "local" (se citado, senão ""), '
            '"tentativas" (o que o servidor já tentou, 1 frase; "" se nada), "setor_id" (int, de SETORES).\n'
            f"SETORES: {json.dumps(setores, ensure_ascii=False)}\nCONVERSA:\n" +
            "\n".join(f"{m['autor']}: {m['texto']}" for m in conversa)}], max_tokens=700))
        if r.get("setor_id") not in {s["id"] for s in setores}:
            r["setor_id"] = setores[0]["id"]
        return {k: str(r.get(k) or "") for k in ("titulo", "descricao", "local", "tentativas")} | {"setor_id": r["setor_id"]}, "llm"
    except Exception as e:  # noqa: BLE001
        log.warning("rascunho LLM falhou (%s)", e)
        return {"titulo": (falas[0] if falas else "")[:70], "descricao": " ".join(falas), "local": "",
                "tentativas": "Orientações do assistente virtual", "setor_id": setores[0]["id"]}, "regras"


@app.get("/servidor", response_class=HTMLResponse)
def servidor(request: Request):
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c)
        conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall() if token else []
        minhas = solicitacoes_do_visitante(request, c)
        chamado = chamado_da_conversa(c, token)
        humano = _contexto_humano(c, token)
    return render(request, "servidor.html", conversa=conversa, minhas=minhas, **humano,
                  chamado_chat=chamado, estado_chat=estado_conversa(token, conversa) if token else "")


@app.post("/servidor/chat", response_class=HTMLResponse)
def servidor_chat(request: Request, pergunta: str = Form(...), estado_chat: str = Form("")):
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c, estado_chat)
        if not token:
            token = secrets.token_urlsafe(16)
            c.execute("INSERT INTO autoatendimento(token,criado_em) VALUES(?,?)", (token, agora()))
        email = email_solicitante_atual(request, c)
        if email:
            c.execute('INSERT OR IGNORE INTO conversa_acessos(token,email) VALUES(?,?)', (token,email))
        c.execute("INSERT INTO auto_msgs(token,autor,texto,criado_em) VALUES(?,?,?,?)", (token, "servidor", pergunta, agora()))
    with db() as c:
        humano = _contexto_humano(c, token)
        if humano["modo"] == "humano" and not chamado_da_conversa(c, token):
            ag = c.execute("SELECT atendente_id FROM autoatendimento WHERE token=?", (token,)).fetchone()
            if ag and ag["atendente_id"]:
                notificar(c, [f"u:{ag['atendente_id']}"], "Nova mensagem do servidor", pergunta, f"/atendente/conversa/{token}")
        if humano["modo"] == "ia" or chamado_da_conversa(c, token):
            resposta, fonte = responder_autoatendimento(c, token, pergunta)
            c.execute("INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
                      (token, "assistente", resposta, fonte, agora()))
        # Com atendente na conversa (ou na fila), o assistente não responde: quem fala é a pessoa.
        conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
        chamado = chamado_da_conversa(c, token)
    estado = estado_conversa(token, conversa)
    resp = render(request, "_servidor_chat.html", conversa=conversa, estado_chat=estado, chamado_chat=chamado, **humano)
    resp.set_cookie("auto_token", token, httponly=True, samesite="lax")
    if len(estado) <= 3800:
        resp.set_cookie("auto_estado", estado, httponly=True, samesite="lax", max_age=86400,
                        secure=request.url.scheme == "https")
    else:
        resp.delete_cookie("auto_estado")
    return resp


@app.post("/servidor/resolvido")
def servidor_resolvido(request: Request, estado_chat: str = Form("")):
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c, estado_chat)
        if not token:
            return redirect("/servidor")
        conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
        if not any(m["autor"] == "assistente" for m in conversa):
            raise HTTPException(400, "Converse com o assistente antes de confirmar a resolução.")
        if chamado_da_conversa(c, token) or conversa[-1]['fonte'] == 'fora_escopo':
            raise HTTPException(409, 'Esta conversa não pode ser contabilizada como resolução sem chamado.')
        c.execute("UPDATE autoatendimento SET resolvido=1 WHERE token=?", (token,))
        minhas = solicitacoes_do_visitante(request, c)
    resp = render(request, "servidor.html", conversa=conversa, minhas=minhas, conversa_resolvida=True,
                  estado_chat=estado_conversa(token, conversa, resolvida=True), conversa_id=token,
                  msg="Resolvido pelo assistente, sem abrir chamado. A conversa foi mantida no histórico.")
    resp.delete_cookie("auto_token")
    resp.delete_cookie("auto_estado")
    return resp


@app.post("/servidor/historico", response_class=HTMLResponse)
def servidor_historico(request: Request, estado_chat: str = Form(...)):
    exige_solicitante(request)
    snapshot = ler_estado_conversa(estado_chat)
    if not snapshot or not snapshot.get("resolvida"):
        raise HTTPException(400, "Conversa inválida. Abra uma conversa salva no histórico.")
    with db() as c:
        minhas = solicitacoes_do_visitante(request, c)
    # Consulta de histórico não altera contadores nem recria atendimentos encerrados.
    return render(request, "servidor.html", conversa=snapshot["msgs"], minhas=minhas, conversa_resolvida=True,
                  estado_chat=estado_chat, conversa_id=snapshot["token"])


@app.post("/servidor/nova")
def servidor_nova(request: Request):
    exige_solicitante(request)
    resp = redirect("/servidor")
    resp.delete_cookie("auto_token")
    resp.delete_cookie("auto_estado")
    return resp


@app.get('/servidor/status', response_class=HTMLResponse)
def servidor_status(request: Request):
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c)
        chamado = chamado_da_conversa(c, token)
    return render(request, '_chat_status.html', chamado_chat=chamado)


# ---------------------------------------------------------------- conversa com atendente humano
def _contexto_humano(c, token):
    """Modo da conversa: 'ia' (assistente), 'aguardando' (pediu atendente) ou 'humano' (atendente assumiu)."""
    row = c.execute("SELECT a.modo, a.atendente_id, u.nome FROM autoatendimento a LEFT JOIN usuarios u ON u.id=a.atendente_id "
                    "WHERE a.token=?", (token,)).fetchone() if token else None
    return {"modo": row["modo"] if row else "ia", "atendente_nome": row["nome"] if row else None}


def _msg_sistema(c, token, texto):
    c.execute("INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
              (token, "sistema", texto, "sistema", agora()))


def _chat_fragmento(request, c, token):
    conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
    return render(request, "_servidor_chat.html", conversa=conversa, estado_chat=estado_conversa(token, conversa),
                  chamado_chat=chamado_da_conversa(c, token), **_contexto_humano(c, token))


@app.post("/servidor/atendente", response_class=HTMLResponse)
def servidor_falar_com_atendente(request: Request, estado_chat: str = Form("")):
    """O servidor pede para falar com uma pessoa: a conversa entra na fila do atendimento."""
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c, estado_chat)
        if not token:
            token = secrets.token_urlsafe(16)
            c.execute("INSERT INTO autoatendimento(token,criado_em) VALUES(?,?)", (token, agora()))
        email = email_solicitante_atual(request, c)
        if email:
            c.execute("INSERT OR IGNORE INTO conversa_acessos(token,email) VALUES(?,?)", (token, email))
        atual = c.execute("SELECT modo, resolvido FROM autoatendimento WHERE token=?", (token,)).fetchone()
        if atual["resolvido"] or chamado_da_conversa(c, token):
            raise HTTPException(409, "Esta conversa já foi encerrada ou virou chamado.")
        if atual["modo"] == "ia":
            c.execute("UPDATE autoatendimento SET modo='aguardando', modo_desde=? WHERE token=?", (agora(), token))
            _msg_sistema(c, token, "Pedido enviado ao Helpdesk. Um atendente vai assumir a conversa; você pode "
                                   "continuar escrevendo e ele verá todo o histórico.")
            notificar(c, _usuarios_com_papel(c, "atendente"), "Conversa aguardando atendente", "Um servidor pediu para falar com uma pessoa.",
                      f"/atendente/conversa/{token}")
        resp = _chat_fragmento(request, c, token)
    resp.set_cookie("auto_token", token, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.get("/servidor/mensagens", response_class=HTMLResponse)
def servidor_mensagens(request: Request):
    """Atualização periódica da conversa quando há atendente envolvido."""
    exige_solicitante(request)
    with db() as c:
        token = _sessao_auto(request, c)
        if not token:
            return HTMLResponse("")
        return _chat_fragmento(request, c, token)


def conversas_na_fila(c):
    return c.execute(
        "SELECT a.token, a.modo, a.modo_desde, a.atendente_id, u.nome atendente, "
        "(SELECT texto FROM auto_msgs m WHERE m.token=a.token AND m.autor='servidor' ORDER BY id LIMIT 1) primeira, "
        "(SELECT texto FROM auto_msgs m WHERE m.token=a.token AND m.autor='servidor' ORDER BY id DESC LIMIT 1) ultima, "
        "(SELECT count(*) FROM auto_msgs m WHERE m.token=a.token) total "
        "FROM autoatendimento a LEFT JOIN usuarios u ON u.id=a.atendente_id "
        "WHERE a.resolvido=0 AND a.modo IN ('aguardando','humano') AND a.solicitacao_id IS NULL "
        "ORDER BY (a.modo='aguardando') DESC, a.modo_desde").fetchall()


@app.get("/atendente/conversas", response_class=HTMLResponse)
def atendente_conversas(request: Request):
    exige_painel(request, "atendente", "gestor")
    with db() as c:
        conversas = conversas_na_fila(c)
    return render(request, "_conversas_fila.html", conversas=conversas)


def _conversa_ou_404(c, token):
    a = c.execute("SELECT * FROM autoatendimento WHERE token=?", (token,)).fetchone()
    if not a:
        raise HTTPException(404, "Conversa não encontrada")
    return a


@app.get("/atendente/conversa/{token}", response_class=HTMLResponse)
def atendente_conversa(request: Request, token: str, fragmento: int = 0):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        msgs = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
        humano = _contexto_humano(c, token)
        email = c.execute("SELECT email FROM conversa_acessos WHERE token=?", (token,)).fetchone()
        chamado = chamado_da_conversa(c, token)
    return render(request, "_conversa_msgs.html" if fragmento else "atendente_conversa.html", u=u, a=a, msgs=msgs,
                  email=email["email"] if email else "", chamado=chamado, **humano)


@app.post("/atendente/conversa/{token}/assumir")
def conversa_assumir(request: Request, token: str):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        if a["resolvido"] or a["solicitacao_id"]:
            raise HTTPException(409, "Conversa já encerrada ou convertida em chamado")
        if a["modo"] == "humano" and a["atendente_id"] != u["id"]:
            raise HTTPException(409, "Outro atendente já assumiu esta conversa")
        if a["modo"] != "humano":
            c.execute("UPDATE autoatendimento SET modo='humano', atendente_id=?, modo_desde=? WHERE token=?",
                      (u["id"], agora(), token))
            _msg_sistema(c, token, f"{u['nome']} assumiu a conversa.")
    return redirect(f"/atendente/conversa/{token}")


@app.post("/atendente/conversa/{token}/responder")
def conversa_responder(request: Request, token: str, texto: str = Form(...)):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        if a["modo"] != "humano" or a["atendente_id"] != u["id"] or a["resolvido"]:
            raise HTTPException(409, "Assuma a conversa antes de responder")
        if texto.strip():
            c.execute("INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
                      (token, "atendente", texto.strip(), "atendente", agora()))
            em = c.execute("SELECT email FROM conversa_acessos WHERE token=?", (token,)).fetchone()
            if em:
                notificar(c, [f"s:{em['email'].lower()}"], f"{u['nome']} respondeu na conversa", texto.strip(), "/servidor")
    return redirect(f"/atendente/conversa/{token}")


@app.post("/atendente/conversa/{token}/devolver")
def conversa_devolver(request: Request, token: str):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        if a["modo"] == "humano" and a["atendente_id"] == u["id"]:
            c.execute("UPDATE autoatendimento SET modo='ia', atendente_id=NULL WHERE token=?", (token,))
            _msg_sistema(c, token, "O atendente devolveu a conversa ao assistente virtual.")
    return redirect("/atendente?msg=Conversa devolvida ao assistente")


@app.post("/atendente/conversa/{token}/encerrar")
def conversa_encerrar(request: Request, token: str):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        if a["modo"] != "humano" or a["atendente_id"] != u["id"]:
            raise HTTPException(409, "Somente quem assumiu a conversa pode encerrá-la")
        c.execute("UPDATE autoatendimento SET resolvido=1, modo='ia' WHERE token=?", (token,))
        _msg_sistema(c, token, f"{u['nome']} encerrou o atendimento por conversa.")
    return redirect("/atendente?msg=Conversa encerrada sem chamado")


@app.post("/atendente/conversa/{token}/chamado")
def conversa_abrir_chamado(request: Request, token: str):
    """Cria a solicitação a partir da conversa; o atendente confere e registra no formulário de sempre."""
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        a = _conversa_ou_404(c, token)
        if a["solicitacao_id"]:
            sid = a["solicitacao_id"]
        else:
            msgs = c.execute("SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
            falas = [m["texto"] for m in msgs if m["autor"] == "servidor"]
            email = c.execute("SELECT email FROM conversa_acessos WHERE token=?", (token,)).fetchone()
            setor = c.execute("SELECT id FROM setores ORDER BY id LIMIT 1").fetchone()["id"]
            conversa = "\n".join(f"{m['autor']}: {m['texto']}" for m in msgs)
            cur = c.execute("INSERT INTO solicitacoes(token,nome,email,secretaria,local,contato,titulo,descricao,tentativas,"
                            "setor_id,via_ia,conversa,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (secrets.token_urlsafe(16), "Servidor (conversa de chat)", email["email"] if email else "",
                             "", "", "", (falas[0] if falas else "Atendimento por chat")[:70],
                             " ".join(falas) or "Conversa de chat", "Conversa com atendente", setor, 1, conversa, agora()))
            sid = cur.lastrowid
            c.execute("UPDATE solicitacoes SET protocolo=? WHERE id=?", (f"SOL-{sid:04d}", sid))
            c.execute("UPDATE autoatendimento SET solicitacao_id=?, modo='ia' WHERE token=?", (sid, token))
            _msg_sistema(c, token, f"{u['nome']} abriu a solicitação SOL-{sid:04d} a partir desta conversa. "
                                   "Você pode consultar o andamento aqui.")
    return redirect(f"/atendente?sol={sid}&msg=Confira os dados do solicitante e registre o chamado")


@app.get('/servidor/dashboard', response_class=HTMLResponse)
def servidor_dashboard():
    """Antiga aba de atendimentos: tudo foi unificado em Chamados."""
    return redirect('/meus-chamados')


@app.get('/servidor/conversas/{token}')
def abrir_conversa(request: Request, token: str):
    exige_solicitante(request)
    with db() as c:
        email = email_solicitante_atual(request,c)
        row = c.execute('SELECT a.* FROM autoatendimento a JOIN conversa_acessos ca ON ca.token=a.token WHERE a.token=? AND ca.email=?', (token,email)).fetchone()
        if not row:
            raise HTTPException(404, 'Conversa não encontrada para este perfil.')
        conversa = c.execute('SELECT * FROM auto_msgs WHERE token=? ORDER BY id',(token,)).fetchall()
        minhas = solicitacoes_do_visitante(request,c)
    if row['resolvido']:
        return render(request,'servidor.html',conversa=conversa,minhas=minhas,conversa_resolvida=True,
                      conversa_id=token,estado_chat=estado_conversa(token,conversa,resolvida=True))
    resp = redirect('/servidor')
    resp.set_cookie('auto_token',token,httponly=True,samesite='lax',secure=request.url.scheme=='https')
    resp.delete_cookie('auto_estado')
    return resp


@app.get("/servidor/solicitar", response_class=HTMLResponse)
def servidor_solicitar_form(request: Request, via: str = ""):
    exige_solicitante(request)
    if via != "ia":
        return redirect("/servidor?msg=O chamado é aberto a partir da conversa com o assistente ou com um atendente.")
    with db() as c:
        setores = c.execute("SELECT * FROM setores").fetchall()
        token = _sessao_auto(request, c) if via == "ia" else None
        if token and chamado_da_conversa(c, token):
            return redirect('/servidor')
        if token and c.execute("SELECT fonte FROM auto_msgs WHERE token=? ORDER BY id DESC LIMIT 1", (token,)).fetchone()['fonte'] == 'fora_escopo':
            raise HTTPException(409, 'Esta conversa está fora do escopo do Helpdesk. Inicie uma nova conversa sobre suporte.')
        rascunho, fonte = rascunho_da_conversa(c, token) if token else ({}, None)
        minhas = solicitacoes_do_visitante(request, c)
    return render(request, "servidor_solicitar.html", setores=setores, r=rascunho, fonte=fonte, via_ia=bool(token),
                  secretarias=SECRETARIAS, minhas=minhas)


@app.post("/servidor/solicitar")
def servidor_solicitar(request: Request, nome: str = Form(...), email: str = Form(...), secretaria: str = Form(...),
                       local: str = Form(...), contato: str = Form(""), titulo: str = Form(...),
                       descricao: str = Form(...), tentativas: str = Form(""), setor_id: int = Form(...),
                       via_ia: int = Form(0), continuar_chat: int = Form(0)):
    exige_solicitante(request)
    nome, email, secretaria = validar_solicitante(nome, email, secretaria)
    with db() as c:
        token_auto = _sessao_auto(request, c) if via_ia else None
        if not token_auto:
            raise HTTPException(403, "A criação manual de chamados não está disponível: use o assistente ou fale com um atendente.")
        if token_auto and chamado_da_conversa(c, token_auto):
            raise HTTPException(409, 'Esta conversa já possui uma solicitação.')
        last = c.execute('SELECT fonte FROM auto_msgs WHERE token=? ORDER BY id DESC LIMIT 1',(token_auto,)).fetchone() if token_auto else None
        if last and last['fonte'] == 'fora_escopo':
            raise HTTPException(409, 'O assunto desta conversa não é uma demanda de Helpdesk.')
        conversa = None
        if token_auto:
            conversa = "\n".join(f"{m['autor']}: {m['texto']}" for m in c.execute(
                "SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id", (token_auto,)))
        token = secrets.token_urlsafe(16)
        cur = c.execute("INSERT INTO solicitacoes(token,nome,email,secretaria,local,contato,titulo,descricao,tentativas,"
                        "setor_id,via_ia,conversa,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (token, nome, email, secretaria, local, contato, titulo, descricao, tentativas, setor_id,
                         int(bool(token_auto)), conversa, agora()))
        c.execute("UPDATE solicitacoes SET protocolo=? WHERE id=?", (f"SOL-{cur.lastrowid:04d}", cur.lastrowid))
        notificar(c, _usuarios_com_papel(c, "atendente"), f"Nova solicitação SOL-{cur.lastrowid:04d}", titulo, f"/atendente?sol={cur.lastrowid}")
        if token_auto:
            c.execute("UPDATE autoatendimento SET solicitacao_id=? WHERE token=?", (cur.lastrowid, token_auto))
        elif continuar_chat:
            token_auto = secrets.token_urlsafe(16)
            c.execute('INSERT INTO autoatendimento(token,solicitacao_id,criado_em) VALUES(?,?,?)', (token_auto,cur.lastrowid,agora()))
            c.execute('INSERT INTO auto_msgs(token,autor,texto,criado_em) VALUES(?,?,?,?)', (token_auto,'servidor',descricao,agora()))
        if token_auto:
            email_atual = email_solicitante_atual(request,c)
            if email_atual:
                c.execute('INSERT OR IGNORE INTO conversa_acessos(token,email) VALUES(?,?)', (token_auto,email_atual))
            c.execute('INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)',
                      (token_auto,'assistente',f'Solicitação SOL-{cur.lastrowid:04d} enviada ao Helpdesk. O histórico continua aqui; você pode consultar o andamento nesta conversa.','sistema',agora()))
    destination = '/servidor?msg=Solicitação enviada ao Helpdesk.' if continuar_chat else f"/servidor/acompanhar/{token}?msg=Solicitação enviada! Um atendente vai conferir e registrar o chamado."
    resp = redirect(destination)
    if token_auto:
        resp.set_cookie('auto_token',token_auto,httponly=True,samesite='lax',secure=request.url.scheme=='https')
    resp.delete_cookie('auto_estado')
    return resp


# ---------------------------------------------------------------- solicitante: "Meus chamados"
# Sem login: a pessoa informa o e-mail usado nos chamados e recebe um link de acesso (envio simulado na tela,
# como o link de validação). O link vale por ACESSO_DIAS dias e só mostra os chamados daquele e-mail.

ACESSO_DIAS = 7
# Linha do progresso exibida ao solicitante (status oficial → passo atual)
PASSOS_SOLICITANTE = ["Recebido", "Em atendimento", "Aguardando sua confirmação", "Concluído"]
PASSO_DO_STATUS = {"novo": 0, "devolvido": 0, "reaberto": 0, "encaminhado": 1, "executado": 2, "concluido": 3}


def email_do_acesso(c, token):
    r = c.execute("SELECT email, criado_em FROM acessos_solicitante WHERE token=?", (token,)).fetchone()
    if not r or _data(r["criado_em"]) < datetime.now() - timedelta(days=ACESSO_DIAS):
        raise HTTPException(404, "Link de acesso inválido ou expirado. Solicite um novo em /meus-chamados.")
    return r["email"]


@app.get("/meus-chamados", response_class=HTMLResponse)
def meus_chamados_form(request: Request):
    exige_solicitante(request)
    token = request.cookies.get("acesso_solicitante")
    if token:
        with db() as c:
            recuperar_acesso_demo(request, c)
            if c.execute("SELECT 1 FROM acessos_solicitante WHERE token=? AND criado_em>=?", (token, (
                    datetime.now() - timedelta(days=ACESSO_DIAS)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone():
                return redirect(f"/meus-chamados/{token}")
    return redirect("/login")


@app.post("/meus-chamados", response_class=HTMLResponse)
def meus_chamados_pedir():
    """O acesso por e-mail digitado foi removido: só entra quem faz login."""
    return redirect("/login?msg=" + quote("Entre com o seu perfil para acompanhar os chamados."))


@app.get("/meus-chamados/{token}", response_class=HTMLResponse)
def meus_chamados(request: Request, token: str):
    with db() as c:
        if request.cookies.get("acesso_solicitante") == token:
            recuperar_acesso_demo(request, c)
        email = email_do_acesso(c, token)
        tarefas = c.execute(
            "SELECT t.*, s.nome area, tp.nome tipo, u.nome executor, "
            "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para IS NOT NULL) ultima_mudanca "
            "FROM tarefas t LEFT JOIN setores s ON s.id=t.setor_id LEFT JOIN tipos tp ON tp.id=t.tipo_id "
            "LEFT JOIN usuarios u ON u.id=t.executor_id WHERE lower(t.email)=? ORDER BY t.criado_em DESC", (email,)).fetchall()
        solicitacoes = c.execute("SELECT * FROM solicitacoes WHERE lower(email)=? AND status<>'registrada' "
                                 "ORDER BY criado_em DESC", (email,)).fetchall()
        conversas = c.execute("SELECT a.*, s.protocolo, (SELECT texto FROM auto_msgs m WHERE m.token=a.token AND autor='servidor' "
                              "ORDER BY id LIMIT 1) titulo FROM autoatendimento a JOIN conversa_acessos ca ON ca.token=a.token "
                              "LEFT JOIN solicitacoes s ON s.id=a.solicitacao_id WHERE ca.email=? ORDER BY a.criado_em DESC", (email,)).fetchall()
    abertos = [t for t in tarefas if t["status"] not in FINAIS]
    historico = [t for t in tarefas if t["status"] in FINAIS]
    perfil = tarefas[0] if tarefas else (solicitacoes[0] if solicitacoes else None)
    resp = render(request, "meus_chamados.html", email=email, perfil=perfil, abertos=abertos, historico=historico,
                  solicitacoes=solicitacoes, conversas=conversas, token=token, passos=PASSOS_SOLICITANTE, passo_do_status=PASSO_DO_STATUS,
                  aguardando=[t for t in abertos if t["status"] == "executado"])
    resp.set_cookie("acesso_solicitante", token, httponly=True, samesite="lax", max_age=ACESSO_DIAS * 86400)
    return resp


@app.get("/meus-chamados-sair")
def meus_chamados_sair():
    resp = redirect("/login")  # sair leva à tela de entrar
    resp.delete_cookie("acesso_solicitante")
    resp.delete_cookie("solicitante_demo")
    return resp


@app.get("/servidor/acompanhar/{token}", response_class=HTMLResponse)
def servidor_acompanhar(request: Request, token: str):
    with db() as c:
        s = c.execute("SELECT * FROM solicitacoes WHERE token=?", (token,)).fetchone()
        if not s:
            raise HTTPException(404, "Solicitação não encontrada")
        t = tarefa_ou_404(c, s["tarefa_id"]) if s["tarefa_id"] else None
        minhas = solicitacoes_do_visitante(request, c)
    return render(request, "servidor_acompanhar.html", s=s, t=t, minhas=minhas)


@app.post("/solicitacoes/{sid}/registrar")
def registrar_solicitacao(request: Request, sid: int, titulo: str = Form(...), descricao: str = Form(...),
                          local: str = Form(...), setor_id: int = Form(...), nome: str = Form(...),
                          email: str = Form(...), secretaria: str = Form(...)):
    """O atendente confere a solicitação do servidor e a registra como tarefa (Req. 1)."""
    u = exige_painel(request, "atendente", "gestor")
    nome, email, secretaria = validar_solicitante(nome, email, secretaria)
    with db() as c:
        s = c.execute("SELECT * FROM solicitacoes WHERE id=? AND status='aguardando'", (sid,)).fetchone()
        if not s:
            raise HTTPException(409, "Solicitação já tratada")
        ts = agora()
        desc = descricao + (f"\nJá tentado pelo servidor: {s['tentativas']}" if s["tentativas"] else "")
        cur = c.execute("INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,contato,email,secretaria,setor_id,"
                        "criado_por,token,criado_em,atualizado_em) VALUES('web',?,?,?,?,?,?,?,?,?,?,?,?)",
                        (titulo, desc, local, nome, s["contato"], email, secretaria, setor_id, u["id"],
                         secrets.token_urlsafe(16), ts, ts))
        tid = cur.lastrowid
        c.execute("UPDATE solicitacoes SET status='registrada', tarefa_id=?, registrada_por=?, registrada_em=? WHERE id=?",
                  (tid, u["nome"], ts, sid))
        registrar_evento(c, tid, u["nome"], "atendente",
                         f"Registrada pelo atendente a partir da solicitação {s['protocolo']} "
                         f"({'autoatendimento com IA' if s['via_ia'] else 'formulário sem IA'})", None, "novo")
    em_segundo_plano(triar, tid)
    return redirect(f"/tarefa/{tid}?msg=Solicitação {s['protocolo']} registrada como tarefa #{tid}. A IA está fazendo a triagem…")


@app.post("/solicitacoes/{sid}/descartar")
def descartar_solicitacao(request: Request, sid: int, motivo: str = Form(...)):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        c.execute("UPDATE solicitacoes SET status='descartada', motivo_descarte=?, registrada_por=?, registrada_em=? "
                  "WHERE id=? AND status='aguardando'", (motivo, u["nome"], agora(), sid))
    return redirect("/atendente?msg=Solicitação encerrada sem abertura de chamado")


# ---------------------------------------------------------------- ranking e configuração (Req. 1 e 5)

@app.get("/ranking", response_class=HTMLResponse)
def ver_ranking(request: Request, temporada: int | None = None, setor_id: int | None = None):
    u = usuario_do_cookie(request, "sess_painel") or usuario_do_cookie(request, "sess_campo")
    if not u:
        return redirect("/login")
    with db() as c:
        temporadas = c.execute("SELECT * FROM temporadas ORDER BY numero DESC").fetchall()
        temporada = temporada or temporada_atual(c)
        individual, equipes = ranking(c, temporada, setor_id)
        meds = {r["id"]: medalhas(c, r["id"]) for r in individual}
        setores = c.execute("SELECT * FROM setores").fetchall()
        rg = regras(c)
    return render(request, "ranking.html", u=u, individual=individual, equipes=equipes, medalhas=meds,
                  temporadas=temporadas, temporada=temporada, setores=setores, setor_id=setor_id, rg=rg,
                  campo="executor" in u["papeis"])


@app.get("/config", response_class=HTMLResponse)
def config(request: Request):
    u = exige_painel(request, "gestor")
    with db() as c:
        ctx = dict(
            u=u, regras_json=json.dumps(regras_configuradas(c), ensure_ascii=False, indent=2),
            rg=regras_configuradas(c), rg_ativas=cfg(c, "regras_ativas", {}),
            setores=c.execute("SELECT * FROM setores").fetchall(),
            equipes=c.execute("SELECT e.*, s.nome setor FROM equipes e JOIN setores s ON s.id=e.setor_id").fetchall(),
            tipos=c.execute("SELECT t.*, s.nome setor FROM tipos t JOIN setores s ON s.id=t.setor_id").fetchall(),
            temporadas=c.execute("SELECT * FROM temporadas ORDER BY numero DESC").fetchall(),
            outbox=c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 20").fetchall(),
            perfis_n={k: c.execute("SELECT count(*) n FROM usuarios WHERE " + w).fetchone()["n"] for k, w in (
                ("atendente", "papeis LIKE '%atendente%'"), ("gestor", "papeis LIKE '%gestor%' AND papeis NOT LIKE '%gestor_tecnico%'"),
                ("gestor_tecnico", "papeis LIKE '%gestor_tecnico%'"), ("executor", "papeis LIKE '%executor%'"))},
            pessoas=c.execute("SELECT u.*, e.nome equipe, s.nome area FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id "
                              "LEFT JOIN setores s ON s.id=u.area_id ORDER BY u.papeis, u.nome").fetchall(),
        )
    return render(request, "config.html", **ctx)


def _numero(f, nome, minimo, maximo, tipo):
    try:
        v = tipo(str(f.get(nome, "")).replace(",", "."))
    except ValueError:
        raise ValueError(f"valor inválido em {nome}")
    if not minimo <= v <= maximo:
        raise ValueError(f"{nome} deve ficar entre {minimo} e {maximo}")
    return v


@app.post("/config/regras/tabela")
async def salvar_regras_tabela(request: Request):
    u = exige_painel(request, "gestor")
    f = await request.form()
    try:
        novas = {
            "pontos_por_complexidade": _numero(f, "pontos_por_complexidade", 0, 200, int),
            "multiplicador_nota": {str(n): _numero(f, f"mult_{n}", 0, 3, float) for n in range(1, 6)},
            "sla_minutos": {p: _numero(f, f"sla_{p}", 1, 100000, int) for p in ("P1", "P2", "P3", "P4")},
            "bonus_sla": _numero(f, "bonus_sla", 0, 200, int),
            "bonus_sem_reabertura": _numero(f, "bonus_sem_reabertura", 0, 200, int),
            "bonus_desbloqueio_gestor": _numero(f, "bonus_desbloqueio_gestor", 0, 200, int),
            "bonus_apoio_colega": _numero(f, "bonus_apoio_colega", 0, 200, int),
        }
    except ValueError as e:
        return redirect(f"/config?msg=Regras inválidas: {e}&aba=pontos")
    ativas = {k: f.get(f"ativa_{k}") == "on" for k in REGRAS_ATIVAVEIS}
    with db() as c:
        set_cfg(c, "regras", novas)
        set_cfg(c, "regras_ativas", ativas)
    log.info("regras de pontuação alteradas por %s", u["nome"])
    return redirect("/config?msg=Regras de pontuação atualizadas&aba=pontos")


@app.post("/config/regras")
def salvar_regras(request: Request, regras_json: str = Form(...)):
    u = exige_painel(request, "gestor")
    try:
        novas = json.loads(regras_json)
        if set(REGRAS_PADRAO) - set(novas):
            raise ValueError(f"faltam chaves: {', '.join(sorted(set(REGRAS_PADRAO) - set(novas)))}")
    except ValueError as e:
        return redirect(f"/config?msg=Regras inválidas: {e}")
    with db() as c:
        set_cfg(c, "regras", novas)
    log.info("regras de pontuação alteradas por %s", u["nome"])
    return redirect("/config?msg=Regras de pontuação atualizadas")


@app.post("/config/temporada")
def nova_temporada(request: Request, nome: str = Form(...)):
    exige_painel(request, "gestor")
    with db() as c:
        n = temporada_atual(c)
        c.execute("UPDATE temporadas SET fim=? WHERE numero=?", (agora(), n))
        c.execute("INSERT INTO temporadas(numero,nome,inicio) VALUES(?,?,?)", (n + 1, nome, agora()))
    return redirect("/ranking?msg=Nova temporada iniciada – placar zerado, histórico preservado")


@app.post("/config/setor")
def novo_setor(request: Request, nome: str = Form(...)):
    exige_painel(request, "gestor")
    with db() as c:
        c.execute("INSERT INTO setores(nome) VALUES(?)", (nome,))
    return redirect("/config?msg=Área criada")


# ---------------------------------------------------------------- pessoas e equipes (gestor)
PAPEIS_PESSOA = {"atendente": "atendente", "executor": "executor", "gestor": "gestor,atendente", "gestor_tecnico": "gestor_tecnico"}


def _int_ou_none(v):
    return int(v) if str(v).strip().isdigit() else None


def _dados_pessoa(c, nome, papel, equipe_id, area_id, competencias):
    nome = nome.strip()
    if not nome or papel not in PAPEIS_PESSOA:
        raise HTTPException(422, "Informe o nome e o perfil da pessoa.")
    if papel == "executor" and not c.execute("SELECT 1 FROM equipes WHERE id=?", (equipe_id,)).fetchone():
        raise HTTPException(422, "Escolha a equipe do técnico.")
    if papel == "gestor_tecnico" and not c.execute("SELECT 1 FROM setores WHERE id=?", (area_id,)).fetchone():
        raise HTTPException(422, "Escolha a área do gestor técnico.")
    return (nome, PAPEIS_PESSOA[papel], equipe_id if papel == "executor" else None,
            area_id if papel == "gestor_tecnico" else None, competencias.strip())


@app.post("/config/pessoa")
def nova_pessoa(request: Request, nome: str = Form(...), papel: str = Form(...), equipe_id: str = Form(""),
                area_id: str = Form(""), competencias: str = Form("")):
    exige_painel(request, "gestor")
    with db() as c:
        d = _dados_pessoa(c, nome, papel, _int_ou_none(equipe_id), _int_ou_none(area_id), competencias)
        c.execute("INSERT INTO usuarios(nome,papeis,equipe_id,area_id,competencias,avatar) VALUES(?,?,?,?,?,'')", d)
    return redirect("/config?msg=Pessoa cadastrada&aba=pessoas")


@app.post("/config/pessoa/{pid}")
def editar_pessoa(request: Request, pid: int, nome: str = Form(...), papel: str = Form(...), equipe_id: str = Form(""),
                  area_id: str = Form(""), competencias: str = Form(""), ativo: int = Form(1)):
    u = exige_painel(request, "gestor")
    with db() as c:
        p = c.execute("SELECT * FROM usuarios WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Pessoa não encontrada")
        d = _dados_pessoa(c, nome, papel, _int_ou_none(equipe_id), _int_ou_none(area_id), competencias)
        if pid == u["id"] and "gestor" not in d[1].split(","):
            raise HTTPException(409, "Você não pode retirar o seu próprio perfil de gestor.")
        c.execute("UPDATE usuarios SET nome=?, papeis=?, equipe_id=?, area_id=?, competencias=?, disponivel=? WHERE id=?",
                  (*d, 1 if ativo else 0, pid))
    return redirect("/config?msg=Cadastro atualizado&aba=pessoas")


@app.post("/config/equipe/{eid}")
def editar_equipe(request: Request, eid: int, nome: str = Form(...), setor_id: int = Form(...)):
    exige_painel(request, "gestor")
    with db() as c:
        if not c.execute("SELECT 1 FROM equipes WHERE id=?", (eid,)).fetchone() or not nome.strip():
            raise HTTPException(422, "Equipe inválida")
        c.execute("UPDATE equipes SET nome=?, setor_id=? WHERE id=?", (nome.strip(), setor_id, eid))
    return redirect("/config?msg=Equipe atualizada&aba=pessoas")


@app.post("/config/equipe")
def nova_equipe(request: Request, nome: str = Form(...), setor_id: int = Form(...)):
    exige_painel(request, "gestor")
    with db() as c:
        c.execute("INSERT INTO equipes(nome,setor_id) VALUES(?,?)", (nome, setor_id))
    return redirect("/config?msg=Equipe criada")


@app.post("/config/tipo")
def novo_tipo(request: Request, nome: str = Form(...), setor_id: int = Form(...), complexidade: int = Form(2),
              palavras: str = Form("")):
    exige_painel(request, "gestor")
    with db() as c:
        c.execute("INSERT INTO tipos(nome,setor_id,complexidade,palavras) VALUES(?,?,?,?)",
                  (nome, setor_id, max(1, min(5, complexidade)), palavras.lower()))
    return redirect("/config?msg=Tipo de tarefa criado")


# ---------------------------------------------------------------- mapa do técnico a caminho (simulado)
@app.get("/api/rastro/{tid}")
def rastro(request: Request, tid: int, token: str = ""):
    """Posição simulada do técnico. Visível ao gestor (sessão) e a quem tem o link do chamado; atendente não."""
    with db() as c:
        t = tarefa_ou_404(c, tid)
        autorizado = bool(token) and (token == t["token"] or c.execute(
            "SELECT 1 FROM solicitacoes WHERE token=? AND tarefa_id=?", (token, tid)).fetchone())
        if not autorizado:
            u = usuario_do_cookie(request, "sess_painel")
            papeis = u["papeis"].split(",") if u else []
            if not u or not ({"gestor", "gestor_tecnico"} & set(papeis)):
                raise HTTPException(403, "Acesso restrito ao gestor ou ao link do chamado")
            exige_acesso(u, t)  # gestor técnico: só chamados da sua área
        ini = c.execute("SELECT criado_em FROM eventos WHERE tarefa_id=? AND para='a_caminho' ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
    dados = rastro_estado(chave_local(t), t["status"], ini["criado_em"] if ini else None)
    return JSONResponse(dados or {"simulado": True, "ativo": False}, headers={"Cache-Control": "no-store"})
