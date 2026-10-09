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
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

DB_PATH = Path(os.getenv("PLATAFORMA_DB", BASE / "plataforma.db"))
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
LEGADO_URL = os.getenv("LEGADO_URL", "http://localhost:8001")
API_KEY_LEGADO = os.getenv("API_KEY_LEGADO", "chave-demo-legado")
SECRET = os.getenv("SESSION_SECRET", "troque-este-segredo-em-producao")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))
LLM_SEM_RACIOCINIO = os.getenv("LLM_SEM_RACIOCINIO", "true").lower() == "true"
VAPID_PEM = BASE / "vapid_private.pem"
VAPID_SUB = os.getenv("VAPID_SUB", "mailto:simot@exemplo.sjp.pr.gov.br")

log = logging.getLogger("missoes")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------- domínio

STATUS = {
    "recebida": ("📥", "Recebida"),
    "atribuida": ("👤", "Atribuída"),
    "a_caminho": ("🚗", "A caminho"),
    "em_execucao": ("🔧", "Em execução"),
    "impedida": ("⛔", "Impedida"),
    "concluida": ("✅", "Concluída – aguardando validação"),
    "resolvida": ("🏆", "Resolvida – confirmada"),
    "reaberta": ("🔁", "Reaberta – pendência"),
}
TRANSICOES = {
    "recebida": {"atribuida"},
    "reaberta": {"atribuida"},
    "atribuida": {"atribuida", "a_caminho"},
    "a_caminho": {"em_execucao"},
    "em_execucao": {"impedida", "concluida"},
    "impedida": {"em_execucao"},
    "concluida": {"resolvida", "reaberta"},
    "resolvida": set(),
}
ATIVOS = ("atribuida", "a_caminho", "em_execucao", "impedida")
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
    "Elétrica": "PR-MP-02 Elétrica (NR-10): 1) Desligar o disjuntor do circuito antes de qualquer intervenção. "
    "2) Conferir lâmpada/reator/tomada com multímetro. 3) Substituir componente. 4) Disjuntor desarmando → não "
    "rearmar repetidamente; acionar eletricista responsável.",
    "Hidráulica": "PR-MP-05 Infiltração/vazamento: 1) Isolar a área e proteger equipamentos. 2) Fechar registro se "
    "houver vazamento ativo. 3) Identificar origem (telhado, calha, tubulação). 4) Registrar fotos para orçamento.",
}

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
    "Elétrica": "Elétrica: NÃO abra tomadas, quadros ou luminárias. Se houver cheiro de queimado, faísca ou choque, "
    "afaste as pessoas e peça atendimento imediatamente. Lâmpada queimada: informe local exato e quantidade.",
    "Hidráulica": "Vazamento/infiltração: afaste equipamentos eletrônicos e papéis da água, isole a área e informe o "
    "local exato. Se houver registro acessível e o vazamento for forte, feche-o.",
}

# ---------------------------------------------------------------- banco

SCHEMA = """
CREATE TABLE IF NOT EXISTS setores(id INTEGER PRIMARY KEY, nome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equipes(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, setor_id INTEGER REFERENCES setores(id));
CREATE TABLE IF NOT EXISTS tipos(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, setor_id INTEGER REFERENCES setores(id),
  complexidade INTEGER NOT NULL DEFAULT 2, palavras TEXT NOT NULL DEFAULT '', base_conhecimento TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, papeis TEXT NOT NULL,
  equipe_id INTEGER REFERENCES equipes(id), competencias TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '🙂');
CREATE TABLE IF NOT EXISTS tarefas(
  id INTEGER PRIMARY KEY, origem TEXT NOT NULL DEFAULT 'web', external_id TEXT,
  titulo TEXT NOT NULL, descricao TEXT NOT NULL, local TEXT NOT NULL DEFAULT '', solicitante TEXT NOT NULL DEFAULT '',
  contato TEXT NOT NULL DEFAULT '', setor_id INTEGER REFERENCES setores(id), tipo_id INTEGER REFERENCES tipos(id),
  prioridade TEXT, status TEXT NOT NULL DEFAULT 'recebida', executor_id INTEGER REFERENCES usuarios(id),
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
CREATE TABLE IF NOT EXISTS solicitacoes(id INTEGER PRIMARY KEY, protocolo TEXT UNIQUE, token TEXT NOT NULL UNIQUE,
  nome TEXT NOT NULL, local TEXT NOT NULL, contato TEXT NOT NULL DEFAULT '', titulo TEXT NOT NULL, descricao TEXT NOT NULL,
  tentativas TEXT NOT NULL DEFAULT '', setor_id INTEGER REFERENCES setores(id), via_ia INTEGER NOT NULL DEFAULT 0,
  conversa TEXT, status TEXT NOT NULL DEFAULT 'aguardando', motivo_descarte TEXT, tarefa_id INTEGER REFERENCES tarefas(id),
  registrada_por TEXT, criado_em TEXT NOT NULL, registrada_em TEXT);
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


def regras(c):
    return cfg(c, "regras", REGRAS_PADRAO)


def temporada_atual(c):
    return c.execute("SELECT max(numero) n FROM temporadas").fetchone()["n"]


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        if c.execute("SELECT count(*) n FROM usuarios").fetchone()["n"] == 0:
            seed(c)


def seed(c):
    c.executemany("INSERT INTO setores(id,nome) VALUES(?,?)", [(1, "TI – Help Desk"), (2, "Manutenção Predial")])
    c.executemany("INSERT INTO equipes(id,nome,setor_id) VALUES(?,?,?)", [
        (1, "Infra de Redes", 1), (2, "Suporte e Almoxarifado TI", 1), (3, "Manutenção Predial", 2)])
    c.executemany("INSERT INTO tipos(id,nome,setor_id,complexidade,palavras,base_conhecimento) VALUES(?,?,?,?,?,?)", [
        (1, "Rede / conectividade", 1, 3, "rede,ponto de rede,internet,cabo,switch,sem conexão,link", "Rede"),
        (2, "Wi-fi", 1, 2, "wi-fi,wifi,sem fio,access point", "Wi-fi"),
        (3, "Computador / sistemas", 1, 2, "computador,pc,monitor,sistema,senha,teclado,lento", "Computador"),
        (4, "Impressora", 1, 1, "impressora,toner,impressão,scanner", "Impressora"),
        (5, "Elétrica", 2, 3, "luz,lâmpada,lampada,tomada,energia,disjuntor,fiação", "Elétrica"),
        (6, "Hidráulica / infiltração", 2, 3, "vazamento,infiltração,infiltracao,goteira,telhado,cano,torneira", "Hidráulica"),
        (7, "Vistoria predial", 2, 2, "vistoria,rachadura,laudo,inspeção", ""),
    ])
    c.executemany("INSERT INTO usuarios(id,nome,papeis,equipe_id,competencias,avatar) VALUES(?,?,?,?,?,?)", [
        (1, "Carlos Lima", "atendente", None, "", "🎧"),
        (2, "Paula Mendes", "gestor,atendente", None, "", "🧭"),
        (3, "Rafael Costa", "executor", 1, "redes, cabeamento, keystone, switch", "🦊"),
        (4, "Diego Santos", "executor", 1, "redes, wi-fi, telefonia, servidores", "🐺"),
        (5, "Bruno Alves", "executor", 2, "computadores, impressoras, almoxarifado, materiais", "🦉"),
        (6, "Juliana Rocha", "executor", 3, "elétrica, hidráulica, vistoria", "🦅"),
    ])
    set_cfg(c, "regras", REGRAS_PADRAO)
    c.execute("INSERT INTO temporadas(numero,nome,inicio,fim) VALUES(1,'Temporada de Setembro','2026-09-01 00:00:00','2026-09-30 23:59:59')")
    c.execute("INSERT INTO temporadas(numero,nome,inicio) VALUES(2,'Temporada de Outubro','2026-10-01 00:00:00')")
    # histórico fictício de pontuação (temporada encerrada e atual) para o ranking não começar vazio
    hist = [(4, 1, 1, 210), (5, 2, 1, 160), (6, 3, 1, 190), (3, 1, 1, 120),
            (4, 1, 2, 140), (5, 2, 2, 90), (6, 3, 2, 110), (3, 1, 2, 60), (2, None, 2, 20)]
    for uid, eq, temp, pts in hist:
        c.execute("INSERT INTO pontos(usuario_id,equipe_id,tarefa_id,temporada,pontos,tipo,descricao,criado_em) "
                  "VALUES(?,?,NULL,?,?,'definitivo','Histórico de missões (dados fictícios)',?)",
                  (uid, eq, temp, pts, agora()))
    # algumas tarefas em andamento para o painel não começar vazio
    exemplos = [
        ("Wi-fi instável na biblioteca", "Wi-fi cai a cada 10 minutos na biblioteca da Escola Municipal Afonso Pena.",
         "Escola Municipal Afonso Pena – Biblioteca", "Marcos Pereira", 1, 2, "P3", "em_execucao", 4),
        ("Computador não liga – recepção", "Computador da recepção não liga desde ontem.",
         "CRAS Centro – Recepção", "Lúcia Andrade", 1, 3, "P3", "atribuida", 4),
        ("Lâmpadas queimadas no corredor", "Três lâmpadas queimadas no corredor do 2º andar.",
         "Paço Municipal – 2º andar", "Roberto Dias", 2, 5, "P4", "a_caminho", 6),
    ]
    for tit, desc, loc, sol, setor, tipo, prio, st, ex in exemplos:
        ts = agora()
        cur = c.execute(
            "INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,setor_id,tipo_id,prioridade,status,executor_id,"
            "criado_por,token,ia_status,ia_fonte,criado_em,atualizado_em) VALUES('web',?,?,?,?,?,?,?,?,?,2,?,'aceita','regras',?,?)",
            (tit, desc, loc, sol, setor, tipo, prio, st, ex, secrets.token_urlsafe(16), ts, ts))
        registrar_evento(c, cur.lastrowid, "Paula Mendes", "gestor", "Tarefa cadastrada e atribuída (dados de exemplo)",
                         None, "atribuida")
        if st != "atribuida":
            registrar_evento(c, cur.lastrowid, "Sistema", "seed", "Andamento de exemplo", "atribuida", st)


# ---------------------------------------------------------------- trilha, status e outbox

def registrar_evento(c, tarefa_id, usuario, papel, texto, de=None, para=None, anexo=None):
    c.execute("INSERT INTO eventos(tarefa_id,usuario,papel,de,para,texto,anexo,criado_em) VALUES(?,?,?,?,?,?,?,?)",
              (tarefa_id, usuario, papel, de, para, texto, anexo, agora()))


def enfileirar_outbox(c, t, status, texto, responsavel):
    """Transactional outbox: a atualização ao sistema de origem é gravada na mesma transação da mudança."""
    if t["origem"] == "web" or not t["external_id"]:
        return
    payload = {"external_id": t["external_id"], "tarefa_id": t["id"], "status": status,
               "status_label": STATUS[status][1], "texto": texto, "responsavel": responsavel, "data": agora()}
    c.execute("INSERT INTO outbox(tarefa_id,origem,payload,criado_em) VALUES(?,?,?,?)",
              (t["id"], t["origem"], json.dumps(payload, ensure_ascii=False), agora()))


def mudar_status(c, t, novo, usuario, papel, texto=""):
    if novo not in TRANSICOES[t["status"]]:
        raise HTTPException(409, f"Transição inválida: {STATUS[t['status']][1]} → {STATUS[novo][1]}")
    c.execute("UPDATE tarefas SET status=?, atualizado_em=? WHERE id=?", (novo, agora(), t["id"]))
    registrar_evento(c, t["id"], usuario["nome"] if usuario else "Demandante", papel, texto, t["status"], novo)
    enfileirar_outbox(c, t, novo, texto, usuario["nome"] if usuario else t["solicitante"])


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

def llm(messages, max_tokens=1200, temperature=0.2):
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY não configurada")
    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT, max_retries=0)
    # Modelos de raciocínio (ex.: Nemotron, Qwen3) gastam tokens "pensando"; desligar deixa a resposta rápida e direta.
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if LLM_SEM_RACIOCINIO else None
    r = client.chat.completions.create(model=LLM_MODEL, messages=messages, temperature=temperature,
                                       max_tokens=max_tokens, extra_body=extra)
    texto = (r.choices[0].message.content or "").strip()
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip()
    if not texto:
        raise RuntimeError("LLM retornou vazio")
    return texto


def extrair_json(texto):
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError("sem JSON na resposta")
    return json.loads(m.group(0))


def carga_executores(c, setor_id=None):
    sql = ("SELECT u.*, e.nome equipe, e.setor_id, (SELECT count(*) FROM tarefas t WHERE t.executor_id=u.id AND "
           f"t.status IN ({','.join('?' * len(ATIVOS))})) carga, "
           "(SELECT count(*) FROM tarefas t WHERE t.executor_id=u.id AND t.status='resolvida') resolvidas "
           "FROM usuarios u JOIN equipes e ON e.id=u.equipe_id WHERE u.papeis LIKE '%executor%'")
    params = list(ATIVOS)
    if setor_id:
        sql += " AND e.setor_id=?"
        params.append(setor_id)
    return c.execute(sql + " ORDER BY carga, u.nome", params).fetchall()


def triagem_por_regras(c, t):
    texto = f"{t['titulo']} {t['descricao']} {t['local']}".lower()
    tipos = c.execute("SELECT * FROM tipos" + (" WHERE setor_id=?" if t["setor_id"] else ""),
                      (t["setor_id"],) if t["setor_id"] else ()).fetchall()
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
    return {
        "tipo_id": melhor["id"], "prioridade": prio, "justificativa": just, "categoria": melhor["nome"],
        "complexidade": melhor["complexidade"], "resumo": t["titulo"], "informacoes_faltantes": faltantes,
        "executor_id": escolhido["id"] if escolhido else None,
        "motivo_executor": (f"Competência compatível e menor carga atual ({escolhido['carga']} missões ativas)."
                            if escolhido else "Nenhum executor disponível no setor."),
    }


def triar(tarefa_id):
    """Triagem assíncrona: tipo, criticidade, informações faltantes e executor sugerido (conferidos pelo gestor)."""
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE id=?", (tarefa_id,)).fetchone()
        tipos = c.execute("SELECT t.id, t.nome, t.complexidade, s.nome setor, s.id setor_id FROM tipos t "
                          "JOIN setores s ON s.id=t.setor_id").fetchall()
        execs = carga_executores(c)
        historico = c.execute("SELECT count(*) n FROM tarefas WHERE local=? AND id<>?", (t["local"], t["id"])).fetchone()["n"]
    fonte = "llm"
    try:
        prompt = (
            "Você faz a triagem de chamados de uma prefeitura. Responda SOMENTE com um objeto JSON com as chaves: "
            '"tipo_id" (int), "prioridade" ("P1" crítica|"P2" alta|"P3" média|"P4" baixa), "justificativa" (1 frase), '
            '"categoria" (texto curto), "complexidade" (1-5), "resumo" (1 frase objetiva), '
            '"informacoes_faltantes" (lista de perguntas que o atendente deveria ter feito; vazia se nada faltar), '
            '"executor_id" (int, do mesmo setor do tipo), "motivo_executor" (1 frase citando competência e carga).\n'
            "Critérios de criticidade: serviço essencial parado ou risco à segurança = P1; unidade de atendimento ao "
            "público (saúde, educação, assistência) afetada = P2; impacto localizado = P3; melhoria = P4.\n\n"
            f"TIPOS: {json.dumps([dict(x) for x in tipos], ensure_ascii=False)}\n"
            f"EXECUTORES: {json.dumps([{k: e[k] for k in ('id', 'nome', 'equipe', 'setor_id', 'competencias', 'carga', 'resolvidas')} for e in execs], ensure_ascii=False)}\n"
            f"CHAMADO: título={t['titulo']!r}; descrição={t['descricao']!r}; local={t['local']!r}; "
            f"solicitante={t['solicitante']!r}; contato={t['contato'] or 'não informado'!r}; "
            f"chamados anteriores no mesmo local={historico}"
        )
        r = extrair_json(llm([{"role": "system", "content": "Você é um assistente de triagem. Responda apenas JSON válido."},
                              {"role": "user", "content": prompt}]))
        ids_tipo = {x["id"]: x for x in tipos}
        if r.get("tipo_id") not in ids_tipo or r.get("prioridade") not in PRIORIDADES:
            raise ValueError("JSON fora do esperado")
        setor_tipo = ids_tipo[r["tipo_id"]]["setor_id"]
        if r.get("executor_id") not in {e["id"] for e in execs if e["setor_id"] == setor_tipo}:
            r["executor_id"] = None
        r["informacoes_faltantes"] = [str(x) for x in (r.get("informacoes_faltantes") or [])][:5]
        r["complexidade"] = max(1, min(5, int(r.get("complexidade") or 2)))
    except Exception as e:  # noqa: BLE001 — fallback garante a demo sem IA externa
        log.warning("triagem LLM falhou (%s); usando regras", e)
        with db() as c:
            r = triagem_por_regras(c, t)
        fonte = "regras"
    with db() as c:
        c.execute("UPDATE tarefas SET ia_status='sugerida', ia_fonte=?, ia_json=?, atualizado_em=? WHERE id=?",
                  (fonte, json.dumps(r, ensure_ascii=False), agora(), tarefa_id))
        registrar_evento(c, tarefa_id, "Assistente IA", "ia",
                         f"Sugestão de triagem: {r.get('categoria')} · {r['prioridade']} – {r.get('justificativa')} "
                         f"(fonte: {'LLM' if fonte == 'llm' else 'regras'}; aguardando conferência do gestor)")


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
        sug += f" ⚠️ Motivo recorrente: {recorrentes} ocorrência(s) anteriores de '{i['motivo']}'."
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
    similares = c.execute("SELECT titulo, relato FROM tarefas WHERE status='resolvida' AND tipo_id=? AND relato IS NOT NULL "
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
        resposta = llm(msgs, max_tokens=1500, temperature=0.3)
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
    ini = c.execute("SELECT max(criado_em) m FROM eventos WHERE tarefa_id=? AND para='atribuida'", (t["id"],)).fetchone()["m"]
    fim = c.execute("SELECT max(criado_em) m FROM eventos WHERE tarefa_id=? AND para='concluida'", (t["id"],)).fetchone()["m"]
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
        m.append(("🎯", "Primeira missão confirmada"))
    if c.execute("SELECT 1 FROM tarefas WHERE executor_id=? AND status='resolvida' AND nota=5", (uid,)).fetchone():
        m.append(("⭐", "Cinco estrelas"))
    if c.execute("SELECT 1 FROM tarefas WHERE executor_id=? AND status='resolvida' AND tipo_id=1", (uid,)).fetchone():
        m.append(("🌐", "Mestre das Redes"))
    if c.execute("SELECT 1 FROM pontos WHERE usuario_id=? AND descricao LIKE 'Apoio%'", (uid,)).fetchone():
        m.append(("🤝", "Parceiro de equipe"))
    if c.execute("SELECT 1 FROM pontos WHERE usuario_id=? AND descricao LIKE 'Desbloqueio%'", (uid,)).fetchone():
        m.append(("🔓", "Destravador"))
    return m


def nivel(pontos_totais):
    for limite, nome in ((600, "Lenda de SJP"), (300, "Guardião"), (100, "Explorador")):
        if pontos_totais >= limite:
            return nome
    return "Recruta"


# ---------------------------------------------------------------- indicadores

def indicadores(c):
    por_status = {r["status"]: r["n"] for r in c.execute("SELECT status, count(*) n FROM tarefas GROUP BY status")}
    tempos_inicio, tempos_conf = [], []
    for r in c.execute(
        "SELECT (SELECT min(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='atribuida') a, "
        "(SELECT min(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='em_execucao') e, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='concluida') c, "
        "(SELECT max(criado_em) FROM eventos WHERE tarefa_id=t.id AND para='resolvida') r FROM tarefas t"):
        if r["a"] and r["e"]:
            tempos_inicio.append(minutos(r["a"], r["e"]))
        if r["c"] and r["r"]:
            tempos_conf.append(minutos(r["c"], r["r"]))
    media = lambda xs: f"{sum(xs) / len(xs):.0f} min" if xs else "–"  # noqa: E731
    csat = c.execute("SELECT avg(nota) m, count(nota) n FROM tarefas WHERE nota IS NOT NULL").fetchone()
    motivos = c.execute("SELECT motivo, count(*) n FROM impedimentos GROUP BY motivo ORDER BY n DESC LIMIT 3").fetchall()
    return {
        "recebidas": sum(por_status.values()),
        "em_execucao": sum(por_status.get(s, 0) for s in ("atribuida", "a_caminho", "em_execucao")),
        "impedidas": por_status.get("impedida", 0),
        "aguardando": por_status.get("concluida", 0),
        "resolvidas": por_status.get("resolvida", 0),
        "reaberturas": c.execute("SELECT coalesce(sum(reaberturas),0) n FROM tarefas").fetchone()["n"],
        "tempo_inicio": media(tempos_inicio),
        "tempo_confirmacao": media(tempos_conf),
        "csat": f"{csat['m']:.1f}★ ({csat['n']})" if csat["n"] else "–",
        "motivos": motivos,
        "outbox_pendente": c.execute("SELECT count(*) n FROM outbox WHERE status='pendente'").fetchone()["n"],
        "auto_resolvidos": c.execute("SELECT count(*) n FROM autoatendimento WHERE resolvido=1").fetchone()["n"],
        "solicitacoes_aguardando": c.execute("SELECT count(*) n FROM solicitacoes WHERE status='aguardando'").fetchone()["n"],
    }


# ---------------------------------------------------------------- push (Web Push / VAPID auto-hospedado)

def chave_publica_vapid():
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    if not VAPID_PEM.exists():
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
templates.env.globals.update(STATUS=STATUS, PRIORIDADES=PRIORIDADES, MOTIVOS=MOTIVOS_IMPEDIMENTO,
                             SISTEMAS=SISTEMAS_ORIGEM, LLM_MODEL=LLM_MODEL)
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
templates.env.filters["hora"] = lambda s: s[11:16] if s else ""
templates.env.filters["datahora"] = lambda s: f"{s[8:10]}/{s[5:7]} {s[11:16]}" if s else ""


def render(request, nome, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    return templates.TemplateResponse(request, nome, ctx)


def assinar(uid):
    return f"{uid}.{hmac.new(SECRET.encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:32]}"


def usuario_do_cookie(request, nome):
    v = request.cookies.get(nome)
    if not v or "." not in v or not hmac.compare_digest(assinar(v.split(".", 1)[0]), v):
        return None
    with db() as c:
        return c.execute("SELECT u.*, e.nome equipe FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id "
                         "WHERE u.id=?", (v.split(".", 1)[0],)).fetchone()


def exige_painel(request, *papeis):
    u = usuario_do_cookie(request, "sess_painel")
    if not u:
        raise HTTPException(303, headers={"Location": "/login"})
    if papeis and not set(papeis) & set(u["papeis"].split(",")):
        raise HTTPException(403, f"Acesso restrito ao(s) papel(is): {', '.join(papeis)}")
    return u


def exige_executor(request):
    u = usuario_do_cookie(request, "sess_campo")
    if not u:
        raise HTTPException(303, headers={"Location": "/login"})
    return u


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


@app.exception_handler(HTTPException)
async def erro_http(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"erro": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><link rel=stylesheet href=/static/pico.min.css>"
                        f"<main class=container><h2>⚠️ {exc.status_code}</h2><p>{exc.detail}</p>"
                        f"<a href='javascript:history.back()'>← Voltar</a> · <a href=/login>Trocar usuário</a></main>",
                        status_code=exc.status_code)


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
        usuarios = c.execute("SELECT u.*, e.nome equipe FROM usuarios u LEFT JOIN equipes e ON e.id=u.equipe_id ORDER BY u.id").fetchall()
    return render(request, "login.html", usuarios=usuarios,
                  painel=usuario_do_cookie(request, "sess_painel"), campo=usuario_do_cookie(request, "sess_campo"))


@app.post("/login")
def login(uid: int = Form(...)):
    with db() as c:
        u = c.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    executor = "executor" in u["papeis"]
    resp = redirect("/campo" if executor else ("/gestor" if "gestor" in u["papeis"] else "/atendente"))
    # Sessões separadas para painel (web) e campo (mobile): permite demonstrar gestor e executor lado a lado.
    resp.set_cookie("sess_campo" if executor else "sess_painel", assinar(u["id"]), httponly=True, samesite="lax")
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
            "INSERT INTO tarefas(origem,external_id,titulo,descricao,local,solicitante,contato,setor_id,token,"
            "criado_em,atualizado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (origem, str(dados["external_id"]), (dados.get("titulo") or dados["descricao"])[:80], dados["descricao"],
             dados.get("local", ""), dados.get("solicitante", ""), dados.get("contato", ""), sistema["setor_id"],
             secrets.token_urlsafe(16), ts, ts))
        tid = cur.lastrowid
        registrar_evento(c, tid, sistema["nome"], "integracao",
                         f"Demanda {dados['external_id']} recebida via API (registrada por {dados.get('atendente', 'atendente')})",
                         None, "recebida")
        enfileirar_outbox(c, tarefa_ou_404(c, tid), "recebida", f"Recebida na plataforma Missões SJP como tarefa #{tid}",
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
def atendente(request: Request):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        setores = c.execute("SELECT * FROM setores").fetchall()
        minhas = c.execute("SELECT * FROM tarefas ORDER BY id DESC LIMIT 15").fetchall()
        solicitacoes = c.execute("SELECT * FROM solicitacoes WHERE status='aguardando' ORDER BY id").fetchall()
    return render(request, "atendente.html", u=u, setores=setores, tarefas=minhas, solicitacoes=solicitacoes)


@app.post("/tarefas")
def criar_tarefa(request: Request, titulo: str = Form(...), descricao: str = Form(...), local: str = Form(""),
                 solicitante: str = Form(...), contato: str = Form(""), setor_id: int = Form(...)):
    """Cadastro direto na web, para setores sem sistema legado (Req. 1)."""
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        ts = agora()
        cur = c.execute("INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,contato,setor_id,criado_por,token,"
                        "criado_em,atualizado_em) VALUES('web',?,?,?,?,?,?,?,?,?,?)",
                        (titulo, descricao, local, solicitante, contato, setor_id, u["id"], secrets.token_urlsafe(16), ts, ts))
        registrar_evento(c, cur.lastrowid, u["nome"], "atendente", "Tarefa cadastrada diretamente na plataforma",
                         None, "recebida")
    em_segundo_plano(triar, cur.lastrowid)
    return redirect(f"/tarefa/{cur.lastrowid}?msg=Tarefa cadastrada. A IA está fazendo a triagem…")


@app.get("/gestor", response_class=HTMLResponse)
def gestor(request: Request):
    u = exige_painel(request, "gestor")
    return render(request, "gestor.html", u=u)


@app.get("/gestor/quadro", response_class=HTMLResponse)
def gestor_quadro(request: Request, setor_id: int | None = None):
    exige_painel(request, "gestor")
    with db() as c:
        sql = ("SELECT t.*, u.nome executor, u.avatar, tp.nome tipo FROM tarefas t LEFT JOIN usuarios u ON u.id=t.executor_id "
               "LEFT JOIN tipos tp ON tp.id=t.tipo_id")
        tarefas = c.execute(sql + (" WHERE t.setor_id=?" if setor_id else "") + " ORDER BY coalesce(t.prioridade,'P9'), t.id",
                            (setor_id,) if setor_id else ()).fetchall()
        ind = indicadores(c)
    colunas = {s: [t for t in tarefas if t["status"] == s] for s in STATUS}
    return render(request, "_quadro.html", colunas=colunas, ind=ind)


@app.get("/tarefa/{tid}", response_class=HTMLResponse)
def tarefa_detalhe(request: Request, tid: int):
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        ctx = dict(
            t=t, u=u, eh_gestor="gestor" in u["papeis"],
            ia=json.loads(t["ia_json"]) if t["ia_json"] else None,
            tipos=c.execute("SELECT t.*, s.nome setor FROM tipos t JOIN setores s ON s.id=t.setor_id").fetchall(),
            executores=carga_executores(c),
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
        )
    return render(request, "tarefa.html", **ctx)


@app.get("/tarefa/{tid}/estado")
def tarefa_estado(request: Request, tid: int, v: str = ""):
    exige_painel(request)
    with db() as c:
        return recarregar_se_mudou(c, tid, v)


@app.post("/tarefa/{tid}/atribuir")
def atribuir(request: Request, tid: int, executor_id: int = Form(...), tipo_id: int = Form(...),
             prioridade: str = Form(...), observacao: str = Form("")):
    u = exige_painel(request, "gestor")
    with db() as c:
        t = tarefa_ou_404(c, tid)
        ia = json.loads(t["ia_json"]) if t["ia_json"] else {}
        ia_status = t["ia_status"]
        if ia_status == "sugerida":
            iguais = (ia.get("executor_id") == executor_id and ia.get("tipo_id") == tipo_id
                      and ia.get("prioridade") == prioridade)
            ia_status = "aceita" if iguais else "editada"
        tipo = c.execute("SELECT * FROM tipos WHERE id=?", (tipo_id,)).fetchone()
        ex = c.execute("SELECT * FROM usuarios WHERE id=?", (executor_id,)).fetchone()
        c.execute("UPDATE tarefas SET executor_id=?, tipo_id=?, setor_id=?, prioridade=?, ia_status=? WHERE id=?",
                  (executor_id, tipo_id, tipo["setor_id"], prioridade, ia_status, tid))
        texto = f"Missão atribuída a {ex['nome']} · {tipo['nome']} · {prioridade} ({PRIORIDADES[prioridade]})"
        if ia_status in ("aceita", "editada") and t["ia_status"] == "sugerida":
            texto += f" · sugestão da IA {'aceita' if ia_status == 'aceita' else 'conferida e editada'} pelo gestor"
        if observacao:
            texto += f" · Obs.: {observacao}"
        mudar_status(c, t, "atribuida", u, "gestor", texto)
    em_segundo_plano(enviar_push, executor_id, f"🛡️ Nova missão {prioridade} – #{tid}",
                     f"{t['titulo']} · {t['local']}", f"/campo/{tid}")
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{tid}?msg=Missão atribuída a {ex['nome']} – notificação push enviada")


@app.post("/tarefa/{tid}/rejeitar-ia")
def rejeitar_ia(request: Request, tid: int):
    u = exige_painel(request, "gestor")
    with db() as c:
        c.execute("UPDATE tarefas SET ia_status='rejeitada', atualizado_em=? WHERE id=?", (agora(), tid))
        registrar_evento(c, tid, u["nome"], "gestor", "Sugestão da IA rejeitada pelo gestor")
    return redirect(f"/tarefa/{tid}")


@app.post("/tarefa/{tid}/retriar")
def retriar(request: Request, tid: int):
    exige_painel(request, "gestor", "atendente")
    with db() as c:
        c.execute("UPDATE tarefas SET ia_status='analisando', atualizado_em=? WHERE id=?", (agora(), tid))
    em_segundo_plano(triar, tid)
    return redirect(f"/tarefa/{tid}")


@app.post("/impedimento/{iid}/resolver")
def resolver_impedimento(request: Request, iid: int, providencia: str = Form(...), apoio_id: int | None = Form(None)):
    u = exige_painel(request, "gestor")
    with db() as c:
        i = c.execute("SELECT * FROM impedimentos WHERE id=? AND aberto=1", (iid,)).fetchone()
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
    em_segundo_plano(enviar_push, t["executor_id"], f"🔓 Impedimento resolvido – #{t['id']}", providencia, f"/campo/{t['id']}")
    em_segundo_plano(processar_outbox)
    return redirect(f"/tarefa/{t['id']}?msg=Impedimento resolvido – executor notificado")


# ---------------------------------------------------------------- executor (web mobile, Req. 3)

@app.get("/campo", response_class=HTMLResponse)
def campo(request: Request):
    u = exige_executor(request)
    return render(request, "campo.html", u=u)


@app.get("/campo/lista", response_class=HTMLResponse)
def campo_lista(request: Request):
    u = exige_executor(request)
    with db() as c:
        tarefas = c.execute("SELECT * FROM tarefas WHERE executor_id=? AND status<>'resolvida' "
                            "ORDER BY CASE status WHEN 'atribuida' THEN 0 ELSE 1 END, coalesce(prioridade,'P9'), id DESC",
                            (u["id"],)).fetchall()
        feitas = c.execute("SELECT * FROM tarefas WHERE executor_id=? AND status='resolvida' ORDER BY id DESC LIMIT 5",
                           (u["id"],)).fetchall()
        temp = temporada_atual(c)
        pts = c.execute("SELECT coalesce(sum(CASE WHEN tipo IN ('definitivo','bonus') THEN pontos END),0) d, "
                        "coalesce(sum(CASE WHEN tipo='provisorio' THEN pontos END),0) p FROM pontos "
                        "WHERE usuario_id=? AND temporada=?", (u["id"], temp)).fetchone()
        total = c.execute("SELECT coalesce(sum(pontos),0) n FROM pontos WHERE usuario_id=? AND tipo IN ('definitivo','bonus')",
                          (u["id"],)).fetchone()["n"]
        meds = medalhas(c, u["id"])
    return render(request, "_campo_lista.html", u=u, tarefas=tarefas, feitas=feitas, pts=pts, nivel=nivel(total),
                  medalhas=meds, ultima=max([t["id"] for t in tarefas if t["status"] == "atribuida"], default=0))


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
        if para not in ("a_caminho", "em_execucao") or t["status"] == "impedida":
            raise HTTPException(409, "Ação não permitida neste estado")
        textos = {"a_caminho": "Missão aceita – executor a caminho do local",
                  "em_execucao": "Executor chegou ao local e iniciou o atendimento"}
        mudar_status(c, t, para, u, "executor", textos[para] + (f" · {observacao}" if observacao else ""))
    em_segundo_plano(processar_outbox)
    msg = "Missão encontrada! Atendimento iniciado 🎯" if para == "em_execucao" else "Boa viagem! Status: a caminho 🚗"
    return redirect(f"/campo/{tid}?msg={msg}")


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
        mudar_status(c, t, "impedida", u, "executor", f"Impedimento: {motivo}" + (f" – {detalhe}" if detalhe else ""))
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
        mudar_status(c, t, "concluida", u, "executor", f"Conclusão informada pelo executor: {relato}")
        if arquivo:
            registrar_evento(c, tid, u["nome"], "executor", "Evidência anexada", anexo=arquivo)
        pts = pontuar_conclusao(c, tarefa_ou_404(c, tid))
    em_segundo_plano(resumir_conclusao, tid)
    em_segundo_plano(processar_outbox)
    return redirect(f"/campo/{tid}?msg=Missão concluída! +{pts} pts provisórios – aguardando confirmação da demandante")


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
    em_segundo_plano(enviar_push, u["id"], "🔔 Notificações ativas", "Você receberá suas novas missões aqui.", "/campo")
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
    return render(request, "validar.html", t=t, executor=executor, linha=linha)


@app.post("/validar/{token}")
def validar(request: Request, token: str, resultado: str = Form(...), nota: int = Form(...), comentario: str = Form("")):
    if resultado not in ("resolvido", "pendente") or not 1 <= nota <= 5:
        raise HTTPException(422, "Resposta inválida")
    if resultado == "pendente" and not comentario.strip():
        return redirect(f"/validar/{token}?msg=Conte o que ainda não está funcionando para reabrirmos o atendimento.")
    with db() as c:
        t = c.execute("SELECT * FROM tarefas WHERE token=?", (token,)).fetchone()
        if not t or t["status"] != "concluida":
            raise HTTPException(409, "Esta tarefa não está aguardando sua confirmação")
        c.execute("UPDATE tarefas SET nota=?, comentario_demandante=? WHERE id=?", (nota, comentario, t["id"]))
        if resultado == "resolvido":
            mudar_status(c, t, "resolvida", None, "demandante",
                         f"Resolução confirmada pela demandante ({nota}★)" + (f": {comentario}" if comentario else ""))
            pontuar_validacao(c, tarefa_ou_404(c, t["id"]), nota)
            msg = "Obrigado! Sua confirmação foi registrada."
        else:
            c.execute("UPDATE tarefas SET reaberturas=reaberturas+1 WHERE id=?", (t["id"],))
            c.execute("UPDATE pontos SET tipo='suspenso' WHERE tarefa_id=? AND tipo='provisorio'", (t["id"],))
            mudar_status(c, t, "reaberta", None, "demandante", f"Pendência indicada pela demandante ({nota}★): {comentario}")
            msg = "Pendência registrada. O atendimento foi reaberto e a equipe vai retomar."
    em_segundo_plano(processar_outbox)
    return redirect(f"/validar/{token}?msg={msg}")


# ---------------------------------------------------------------- servidor: autoatendimento com IA (web)
# O servidor conversa com a IA antes de ligar para o Help Desk. Se não resolver, envia uma SOLICITAÇÃO, que um
# atendente confere e registra como tarefa (Req. 1: a abertura é feita por atendente ou responsável autorizado).

def _sessao_auto(request, c):
    token = request.cookies.get("auto_token")
    if token and c.execute("SELECT 1 FROM autoatendimento WHERE token=? AND resolvido=0 AND solicitacao_id IS NULL",
                           (token,)).fetchone():
        return token
    return None


def responder_autoatendimento(c, token, pergunta):
    historico = c.execute("SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id DESC LIMIT 10", (token,)).fetchall()
    msgs = [{"role": "system", "content":
             "Você é o Assistente Virtual do Help Desk da Prefeitura de São José dos Pinhais, atendendo servidores "
             "municipais que NÃO são técnicos. Objetivo: resolver problemas simples com orientações seguras antes que "
             "precisem abrir um chamado. Regras: linguagem simples e cordial; no máximo 4 passos curtos por resposta; "
             "faça no máximo 1 pergunta por vez para entender o problema (o que aconteceu, onde, desde quando, afeta "
             "outras pessoas?); nunca peça senhas; nunca oriente abrir equipamentos, tomadas ou quadros elétricos; em "
             "risco à segurança (fogo, choque, vazamento forte) oriente afastar as pessoas e abrir a solicitação na hora. "
             "Se o problema foi resolvido, peça que clique em 'Resolvido'. Se não for possível resolver sozinho, diga "
             "para clicar em 'Abrir solicitação' — o atendente receberá o resumo desta conversa. Responda em português, "
             "em texto simples, sem markdown.\n"
             f"ORIENTAÇÕES PERMITIDAS: {json.dumps(BASE_AUTOATENDIMENTO, ensure_ascii=False)}"}]
    for h in reversed(historico):
        msgs.append({"role": "user" if h["autor"] == "servidor" else "assistant", "content": h["texto"]})
    msgs.append({"role": "user", "content": pergunta})
    try:
        resposta = llm(msgs, max_tokens=800, temperature=0.3)
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
    with db() as c:
        token = _sessao_auto(request, c)
        conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall() if token else []
    return render(request, "servidor.html", conversa=conversa)


@app.post("/servidor/chat", response_class=HTMLResponse)
def servidor_chat(request: Request, pergunta: str = Form(...)):
    with db() as c:
        token = _sessao_auto(request, c)
        if not token:
            token = secrets.token_urlsafe(16)
            c.execute("INSERT INTO autoatendimento(token,criado_em) VALUES(?,?)", (token, agora()))
        c.execute("INSERT INTO auto_msgs(token,autor,texto,criado_em) VALUES(?,?,?,?)", (token, "servidor", pergunta, agora()))
    with db() as c:
        resposta, fonte = responder_autoatendimento(c, token, pergunta)
        c.execute("INSERT INTO auto_msgs(token,autor,texto,fonte,criado_em) VALUES(?,?,?,?,?)",
                  (token, "assistente", resposta, fonte, agora()))
        conversa = c.execute("SELECT * FROM auto_msgs WHERE token=? ORDER BY id", (token,)).fetchall()
    resp = render(request, "_servidor_chat.html", conversa=conversa)
    resp.set_cookie("auto_token", token, httponly=True, samesite="lax")
    return resp


@app.post("/servidor/resolvido")
def servidor_resolvido(request: Request):
    with db() as c:
        token = _sessao_auto(request, c)
        if token:
            c.execute("UPDATE autoatendimento SET resolvido=1 WHERE token=?", (token,))
    resp = redirect("/servidor?msg=Que bom que deu certo! 🎉 Nenhum chamado foi necessário. Obrigado por usar o assistente.")
    resp.delete_cookie("auto_token")
    return resp


@app.post("/servidor/nova")
def servidor_nova():
    resp = redirect("/servidor")
    resp.delete_cookie("auto_token")
    return resp


@app.get("/servidor/solicitar", response_class=HTMLResponse)
def servidor_solicitar_form(request: Request, via: str = ""):
    with db() as c:
        setores = c.execute("SELECT * FROM setores").fetchall()
        token = _sessao_auto(request, c) if via == "ia" else None
        rascunho, fonte = rascunho_da_conversa(c, token) if token else ({}, None)
    return render(request, "servidor_solicitar.html", setores=setores, r=rascunho, fonte=fonte, via_ia=bool(token))


@app.post("/servidor/solicitar")
def servidor_solicitar(request: Request, nome: str = Form(...), local: str = Form(...), contato: str = Form(""),
                       titulo: str = Form(...), descricao: str = Form(...), tentativas: str = Form(""),
                       setor_id: int = Form(...), via_ia: int = Form(0)):
    with db() as c:
        token_auto = _sessao_auto(request, c) if via_ia else None
        conversa = None
        if token_auto:
            conversa = "\n".join(f"{m['autor']}: {m['texto']}" for m in c.execute(
                "SELECT autor, texto FROM auto_msgs WHERE token=? ORDER BY id", (token_auto,)))
        token = secrets.token_urlsafe(16)
        cur = c.execute("INSERT INTO solicitacoes(token,nome,local,contato,titulo,descricao,tentativas,setor_id,via_ia,"
                        "conversa,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (token, nome, local, contato, titulo, descricao, tentativas, setor_id, int(bool(token_auto)),
                         conversa, agora()))
        c.execute("UPDATE solicitacoes SET protocolo=? WHERE id=?", (f"SOL-{cur.lastrowid:04d}", cur.lastrowid))
        if token_auto:
            c.execute("UPDATE autoatendimento SET solicitacao_id=? WHERE token=?", (cur.lastrowid, token_auto))
    resp = redirect(f"/servidor/acompanhar/{token}?msg=Solicitação enviada! Um atendente vai conferir e registrar o chamado.")
    resp.delete_cookie("auto_token")
    return resp


@app.get("/servidor/acompanhar/{token}", response_class=HTMLResponse)
def servidor_acompanhar(request: Request, token: str):
    with db() as c:
        s = c.execute("SELECT * FROM solicitacoes WHERE token=?", (token,)).fetchone()
        if not s:
            raise HTTPException(404, "Solicitação não encontrada")
        t = tarefa_ou_404(c, s["tarefa_id"]) if s["tarefa_id"] else None
    return render(request, "servidor_acompanhar.html", s=s, t=t)


@app.post("/solicitacoes/{sid}/registrar")
def registrar_solicitacao(request: Request, sid: int, titulo: str = Form(...), descricao: str = Form(...),
                          local: str = Form(...), setor_id: int = Form(...)):
    """O atendente confere a solicitação do servidor e a registra como tarefa (Req. 1)."""
    u = exige_painel(request, "atendente", "gestor")
    with db() as c:
        s = c.execute("SELECT * FROM solicitacoes WHERE id=? AND status='aguardando'", (sid,)).fetchone()
        if not s:
            raise HTTPException(409, "Solicitação já tratada")
        ts = agora()
        desc = descricao + (f"\nJá tentado pelo servidor: {s['tentativas']}" if s["tentativas"] else "")
        cur = c.execute("INSERT INTO tarefas(origem,titulo,descricao,local,solicitante,contato,setor_id,criado_por,token,"
                        "criado_em,atualizado_em) VALUES('web',?,?,?,?,?,?,?,?,?,?)",
                        (titulo, desc, local, s["nome"], s["contato"], setor_id, u["id"], secrets.token_urlsafe(16), ts, ts))
        tid = cur.lastrowid
        c.execute("UPDATE solicitacoes SET status='registrada', tarefa_id=?, registrada_por=?, registrada_em=? WHERE id=?",
                  (tid, u["nome"], ts, sid))
        registrar_evento(c, tid, u["nome"], "atendente",
                         f"Registrada pelo atendente a partir da solicitação {s['protocolo']} "
                         f"({'autoatendimento com IA' if s['via_ia'] else 'formulário sem IA'})", None, "recebida")
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
            u=u, regras_json=json.dumps(regras(c), ensure_ascii=False, indent=2),
            setores=c.execute("SELECT * FROM setores").fetchall(),
            equipes=c.execute("SELECT e.*, s.nome setor FROM equipes e JOIN setores s ON s.id=e.setor_id").fetchall(),
            tipos=c.execute("SELECT t.*, s.nome setor FROM tipos t JOIN setores s ON s.id=t.setor_id").fetchall(),
            temporadas=c.execute("SELECT * FROM temporadas ORDER BY numero DESC").fetchall(),
            outbox=c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 20").fetchall(),
        )
    return render(request, "config.html", **ctx)


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
    return redirect("/config?msg=Setor criado")


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
