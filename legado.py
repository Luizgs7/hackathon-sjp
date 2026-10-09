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
from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
DB_PATH = Path(os.getenv("LEGADO_DB", BASE / "legado.db"))
PLATAFORMA_URL = os.getenv("PLATAFORMA_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY_LEGADO", "chave-demo-legado")
ESTADO = {"online": True}

app = FastAPI(title="SisChamados Legado (simulado)")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


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


CSS = """
body{font-family:Verdana,Arial,sans-serif;font-size:13px;background:#d4d0c8;margin:0;color:#000}
.barra{background:linear-gradient(#0a246a,#3a6ea5);color:#fff;padding:6px 10px;font-weight:bold}
.conteudo{padding:10px;max-width:1100px}
fieldset{background:#ece9d8;border:2px groove #fff;margin-bottom:10px}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #808080;padding:4px;vertical-align:top}
th{background:#ece9d8;text-align:left}input,textarea{font-family:inherit;font-size:13px}
.msg{background:#ffffe1;border:1px solid #000;padding:6px;margin-bottom:8px}
.off{background:#c00;color:#fff;padding:6px;font-weight:bold}.on{background:#080;color:#fff;padding:6px;font-weight:bold}
.hist{font-size:11px;color:#333}button{font-family:inherit}
"""


@app.get("/", response_class=HTMLResponse)
def inicio(msg: str = ""):
    return f"""<!doctype html><html lang=pt-BR><head><meta charset=utf-8><title>SisChamados Legado</title>
<style>{CSS}</style><script src="/static/htmx.min.js"></script></head><body>
<div class=barra>SisChamados v2.3 — Help Desk / Prefeitura Municipal (SISTEMA LEGADO SIMULADO)</div>
<div class=conteudo>
{f'<div class=msg>{html.escape(msg)}</div>' if msg else ''}
<form method=post action=/alternar style="margin-bottom:8px">
 <span class="{'on' if ESTADO['online'] else 'off'}">Recebimento de atualizações: {'ONLINE' if ESTADO['online'] else 'FORA DO AR (simulado)'}</span>
 <button>{'Simular sistema fora do ar' if ESTADO['online'] else 'Religar sistema'}</button>
</form>
<fieldset><legend><b>Registrar chamado (atendente)</b></legend>
<form method=post action=/chamados>
<table><tr><td>Solicitante:</td><td><input name=solicitante size=30 required value="Ana Souza"></td>
<td>Telefone:</td><td><input name=telefone size=15 value="(41) 3381-0000 r.214"></td></tr>
<tr><td>Unidade/Local:</td><td colspan=3><input name=unidade size=70 required value="UBS Vila Nova – Sala de vacinação"></td></tr>
<tr><td>Descrição:</td><td colspan=3><textarea name=descricao rows=2 cols=80 required>Ponto de rede da sala de vacinação não funciona. Computador sem acesso ao sistema de vacinação.</textarea></td></tr>
<tr><td>Atendente:</td><td><input name=atendente value="Carlos Lima"></td><td colspan=2><button><b>Gravar chamado</b></button></td></tr>
</table></form></fieldset>
<fieldset><legend><b>Chamados</b> (atualiza a cada 3 s)</legend>
<div hx-get=/lista hx-trigger="load, every 3s"></div></fieldset>
<p class=hist>Integração: envia para {PLATAFORMA_URL}/api/integracao/legado/tarefas · recebe em /api/atualizacoes</p>
</div></body></html>"""


@app.get("/lista", response_class=HTMLResponse)
def lista():
    with db() as c:
        chamados = c.execute("SELECT * FROM chamados ORDER BY id DESC").fetchall()
        atual = {}
        for a in c.execute("SELECT * FROM atualizacoes ORDER BY sequencia"):
            atual.setdefault(a["numero"], []).append(a)
    linhas = []
    for ch in chamados:
        hist = "".join(f"<div>[{a['recebido_em']}] seq {a['sequencia']} · <b>{html.escape(a['situacao'])}</b> — "
                       f"{html.escape(a['texto'] or '')} ({html.escape(a['responsavel'] or '')})</div>"
                       for a in atual.get(ch["numero"], []))
        linhas.append(
            f"<tr><td><b>{ch['numero']}</b><br>{ch['criado_em']}</td><td>{html.escape(ch['solicitante'])}<br>"
            f"{html.escape(ch['unidade'])}</td><td>{html.escape(ch['descricao'])}<div class=hist>{hist or 'Sem atualizações.'}</div></td>"
            f"<td><b>{html.escape(ch['situacao'])}</b><br>{'Tarefa #' + str(ch['tarefa_id']) if ch['enviado'] else 'NÃO ENVIADO'}</td>"
            f"<td><form method=post action='/chamados/{ch['numero']}/reenviar'><button>Reenviar</button></form></td></tr>")
    return ("<table><tr><th>Nº</th><th>Solicitante/Local</th><th>Descrição / histórico recebido da plataforma</th>"
            "<th>Situação</th><th></th></tr>" + ("".join(linhas) or "<tr><td colspan=5>Nenhum chamado.</td></tr>") + "</table>")
