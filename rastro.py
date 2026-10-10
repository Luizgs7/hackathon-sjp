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


def distancia_km(tarefa_id):
    """Extensão do percurso simulado (ida), em km."""
    pts = caminho(tarefa_id)
    total = 0.0
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        dy = (la2 - la1) * 111.0
        dx = (lo2 - lo1) * 111.0 * math.cos(math.radians((la1 + la2) / 2))
        total += math.hypot(dx, dy)
    return round(total, 2)


def km_entre(a, b):
    """Distância aproximada em km entre dois pontos (lat, lng) próximos."""
    dy = (b[0] - a[0]) * 111.0
    dx = (b[1] - a[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


PESO_PRIO = {"P1": 0.2, "P2": 0.55, "P3": 1.0, "P4": 1.35}  # criticidade encurta a distância "percebida"
VELOCIDADE_KMH = 25  # deslocamento urbano simulado


def km_rua(a, b):
    """Distância por ruas em 'L' (mesmo desenho do percurso simulado)."""
    dy = abs(b[0] - a[0]) * 111.0
    dx = abs(b[1] - a[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return dx + dy


def caminho_rua(a, b):
    return [list(a), [a[0], b[1]], list(b)]


CRITERIOS = {"misto": "Misto (criticidade e distância)", "distancia": "Menor distância", "criticidade": "Criticidade primeiro"}


def calcular_trechos(origem, ordem):
    """Distância por trecho, acumulada e tempo de chegada para uma sequência de paradas já definida."""
    atual, acum, saida = tuple(origem), 0.0, []
    for p in ordem:
        trecho = km_rua(atual, p["ponto"])
        acum += trecho
        saida.append({**p, "trecho_km": round(trecho, 1), "acumulado_km": round(acum, 1),
                      "eta_min": max(1, round(acum / VELOCIDADE_KMH * 60))})
        atual = tuple(p["ponto"])
    return saida


def ordenar_rota(origem, paradas, criterio="misto"):
    """Escolhe a próxima parada a cada passo. misto: vizinho mais próximo ponderado pela criticidade;
    distancia: só a distância; criticidade: P1 antes de P2..., e dentro do mesmo nível o mais próximo."""
    def custo(atual, p):
        d = km_rua(atual, p["ponto"])
        prio = p.get("prio") or "P3"
        if criterio == "distancia":
            return (d, p["id"])
        if criterio == "criticidade":
            return (prio, d, p["id"])
        return (d * PESO_PRIO.get(prio, 1.0), p["id"])
    restantes, atual, ordem = list(paradas), tuple(origem), []
    while restantes:
        melhor = min(restantes, key=lambda p: custo(atual, p))
        ordem.append(melhor)
        restantes.remove(melhor)
        atual = tuple(melhor["ponto"])
    return calcular_trechos(origem, ordem)
