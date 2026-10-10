"""Rota simulada do técnico a caminho. Sem GPS real: a posição é calculada a partir do horário em que o
técnico marcou "a caminho", então qualquer instância do servidor responde igual (sem estado compartilhado)."""
import hashlib
import math
from datetime import datetime

BASE = (-25.5345, -49.2063)       # ponto de partida fictício (região central de São José dos Pinhais)
DURACAO_S = 360                   # a viagem simulada dura 6 minutos
RASTREAVEIS = ("a_caminho",)
CHEGOU = ("em_execucao", "executado", "concluido")


def destino(tarefa_id):
    """Destino fictício 0,8–3 km da base, determinístico por chamado."""
    h = hashlib.sha256(f"rastro:{tarefa_id}".encode()).digest()
    ang = h[0] / 255 * 2 * math.pi
    km = 0.8 + h[1] / 255 * 2.2
    return (BASE[0] + km * math.sin(ang) / 111.0,
            BASE[1] + km * math.cos(ang) / (111.0 * math.cos(math.radians(BASE[0]))))


def caminho(tarefa_id):
    """Percurso em 'L' (como ruas em quadra), da base até o destino."""
    fim = destino(tarefa_id)
    return [BASE, (BASE[0], fim[1]), fim]


def _ponto(pts, frac):
    seg = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    alvo = frac * sum(seg)
    for (a, b), s in zip(zip(pts, pts[1:]), seg):
        if alvo <= s or s == 0:
            f = alvo / s if s else 0
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        alvo -= s
    return pts[-1]


def estado(tarefa_id, status, inicio, agora=None):
    """inicio: 'AAAA-MM-DD HH:MM:SS' do evento a_caminho (ou None). Retorna None quando não há o que mostrar."""
    if status not in RASTREAVEIS + CHEGOU or not inicio:
        return None
    pts = caminho(tarefa_id)
    if status in CHEGOU:
        frac = 1.0
    else:
        dec = ((agora or datetime.now()) - datetime.strptime(inicio, "%Y-%m-%d %H:%M:%S")).total_seconds()
        frac = min(max(dec / DURACAO_S, 0.0), 1.0)
    lat, lng = _ponto(pts, frac)
    restante = 0 if frac >= 1 else max(1, math.ceil((1 - frac) * DURACAO_S / 60))
    return {"simulado": True, "status": status, "chegou": frac >= 1, "progresso": round(frac, 3),
            "eta_min": restante, "posicao": [round(lat, 6), round(lng, 6)],
            "caminho": [[round(a, 6), round(b, 6)] for a, b in pts], "origem": list(BASE),
            "destino": [round(pts[-1][0], 6), round(pts[-1][1], 6)]}
