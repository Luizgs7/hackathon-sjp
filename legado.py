"""SisChamados Legado — sistema de Help Desk SIMULADO (porta 8001).

Representa o sistema existente da Prefeitura: o atendente registra o chamado aqui, o legado envia a
demanda para a plataforma Missões SJP (API) e recebe de volta as atualizações (andamento, impedimentos,
conclusão, validação). Tem um interruptor "fora do ar" para demonstrar a fila de sincronização.
"""
import html
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
DB_PATH = Path(os.getenv("LEGADO_DB", BASE / "legado.db"))
PLATAFORMA_URL = os.getenv("PLATAFORMA_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY_LEGADO", "chave-demo-legado")
ESTADO = {"online": True}

app = FastAPI(title="SisChamados Legado (simulado)")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["datahora"] = lambda s: f"{s[8:10]}/{s[5:7]} {s[11:16]}" if s else ""


@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


with db() as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS chamados(id INTEGER PRIMARY KEY, numero TEXT UNIQUE, solicitante TEXT, telefone TEXT,
      unidade TEXT, descricao TEXT, atendente TEXT, situacao TEXT NOT NULL DEFAULT 'Aberto', enviado INTEGER NOT NULL DEFAULT 0,
      tarefa_id INTEGER, criado_em TEXT);
    CREATE TABLE IF NOT EXISTS atualizacoes(id INTEGER PRIMARY KEY, numero TEXT, sequencia INTEGER, situacao TEXT,
      texto TEXT, responsavel TEXT, recebido_em TEXT, UNIQUE(numero, sequencia));
    """)


def agora():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def enviar(numero):
    with db() as c:
        ch = c.execute("SELECT * FROM chamados WHERE numero=?", (numero,)).fetchone()
    payload = {"external_id": ch["numero"], "titulo": ch["descricao"][:70], "descricao": ch["descricao"],
               "local": ch["unidade"], "solicitante": ch["solicitante"], "contato": ch["telefone"],
               "atendente": ch["atendente"]}
    r = httpx.post(f"{PLATAFORMA_URL}/api/integracao/legado/tarefas", json=payload, headers={"X-Api-Key": API_KEY}, timeout=10)
    r.raise_for_status()
    dados = r.json()
    with db() as c:
        c.execute("UPDATE chamados SET enviado=1, tarefa_id=? WHERE numero=?", (dados["id"], numero))
    return dados


@app.post("/chamados")
def registrar(solicitante: str = Form(...), telefone: str = Form(""), unidade: str = Form(...),
              descricao: str = Form(...), atendente: str = Form("Carlos Lima")):
    with db() as c:
        n = c.execute("SELECT count(*) n FROM chamados").fetchone()["n"]
        numero = f"LG-{2031 + n}"
        c.execute("INSERT INTO chamados(numero,solicitante,telefone,unidade,descricao,atendente,criado_em) "
                  "VALUES(?,?,?,?,?,?,?)", (numero, solicitante, telefone, unidade, descricao, atendente, agora()))
    try:
        d = enviar(numero)
        msg = f"Chamado {numero} registrado e enviado à plataforma (tarefa #{d['id']})."
    except Exception as e:  # noqa: BLE001
        msg = f"Chamado {numero} registrado, mas a plataforma não respondeu ({e.__class__.__name__}). Use 'Reenviar'."
    return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)


@app.post("/chamados/{numero}/reenviar")
def reenviar(numero: str):
    try:
        d = enviar(numero)
        msg = (f"Reenvio de {numero}: a plataforma reconheceu a demanda já existente (tarefa #{d['id']}) — nenhum registro duplicado."
               if d.get("duplicada") else f"{numero} enviado (tarefa #{d['id']}).")
    except Exception as e:  # noqa: BLE001
        msg = f"Falha no envio: {e.__class__.__name__}"
    return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)


@app.post("/alternar")
def alternar():
    ESTADO["online"] = not ESTADO["online"]
    return RedirectResponse("/", status_code=303)


@app.post("/api/atualizacoes")
def receber_atualizacao(dados: dict, x_api_key: str = Header(...)):
    """Recebe atualizações da plataforma. Idempotente por (numero, sequencia)."""
    if not ESTADO["online"]:
        raise HTTPException(503, "Sistema legado fora do ar (simulação)")
    if x_api_key != API_KEY:
        raise HTTPException(401, "Chave inválida")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO atualizacoes(numero,sequencia,situacao,texto,responsavel,recebido_em) "
                  "VALUES(?,?,?,?,?,?)", (dados["external_id"], dados["sequencia"], dados["status_label"],
                                          dados.get("texto", ""), dados.get("responsavel", ""), agora()))
        c.execute("UPDATE chamados SET situacao=? WHERE numero=?", (dados["status_label"], dados["external_id"]))
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "df/legado.html", {"msg": msg, "online": ESTADO["online"], "plataforma": PLATAFORMA_URL})


@app.get("/lista", response_class=HTMLResponse)
def lista(request: Request):
    with db() as c:
        chamados = c.execute("SELECT * FROM chamados ORDER BY id DESC").fetchall()
        atual = {}
        for a in c.execute("SELECT * FROM atualizacoes ORDER BY sequencia"):
            atual.setdefault(a["numero"], []).append(a)
    return templates.TemplateResponse(request, "df/_legado_lista.html", {"chamados": chamados, "atualizacoes": atual})