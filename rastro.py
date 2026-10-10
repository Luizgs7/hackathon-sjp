"""Rota simulada do técnico a caminho. Sem GPS real: a posição é calculada a partir do horário em que o
técnico marcou "a caminho", então qualquer instância do servidor responde igual (sem estado compartilhado)."""
import hashlib
import math
from datetime import datetime

BASE = (-25.5345, -49.2063)       # ponto de partida fictício (região central de São José dos Pinhais)
DURACAO_S = 360                   # a viagem simulada dura 6 minutos
RASTREAVEIS = ("a_caminho",)
CHEGOU = ("em_execucao", "executado", "concluido")


# Pontos reais em vias de São José dos Pinhais (obtidos do OpenStreetMap): os destinos simulados nunca caem em rio ou mata.
PONTOS = [
    (-25.561541, -49.216253), (-25.561499, -49.201350), (-25.561613, -49.196302),
    (-25.561604, -49.186192), (-25.561567, -49.176344), (-25.557073, -49.221477),
    (-25.557023, -49.216284), (-25.557080, -49.201607), (-25.556980, -49.191065),
    (-25.556949, -49.186061), (-25.552351, -49.211410), (-25.552609, -49.201351),
    (-25.552470, -49.196292), (-25.552590, -49.191183), (-25.552362, -49.176217),
    (-25.547985, -49.206414), (-25.548267, -49.196438), (-25.548263, -49.191238),
    (-25.547839, -49.181254), (-25.548069, -49.176203), (-25.543558, -49.216543),
    (-25.543558, -49.206171), (-25.543474, -49.201291), (-25.543518, -49.191355),
    (-25.543498, -49.186326), (-25.543301, -49.181240), (-25.538977, -49.221430),
    (-25.538955, -49.211416), (-25.539043, -49.206112), (-25.538734, -49.191417),
    (-25.539144, -49.186398), (-25.534499, -49.216462), (-25.534610, -49.196033),
    (-25.534435, -49.191390), (-25.534773, -49.186424), (-25.525509, -49.211446),
    (-25.525395, -49.206313), (-25.525503, -49.201274), (-25.525440, -49.196130),
    (-25.525429, -49.186272), (-25.520844, -49.226339), (-25.520994, -49.211363),
    (-25.520968, -49.206144), (-25.520988, -49.201323), (-25.520933, -49.196385),
    (-25.520824, -49.191167), (-25.521008, -49.186304), (-25.521052, -49.176262),
    (-25.516423, -49.226297), (-25.516687, -49.206273), (-25.516479, -49.201194),
    (-25.516549, -49.191237), (-25.516413, -49.186263), (-25.512040, -49.236412),
    (-25.511926, -49.231135), (-25.512010, -49.226252), (-25.512137, -49.196474),
    (-25.511965, -49.191339), (-25.512017, -49.186279), (-25.511968, -49.181351),
    (-25.507437, -49.231279), (-25.507498, -49.226331), (-25.507386, -49.191437),
    (-25.507543, -49.181179), (-25.507679, -49.176544),
]


def destino(tarefa_id):
    """Destino fictício em uma via real, 0,5–3 km da base, determinístico por chamado."""
    h = hashlib.sha256(f"rastro:{tarefa_id}".encode()).digest()
    return PONTOS[int.from_bytes(h[:2], "big") % len(PONTOS)]


def caminho(tarefa_id):
    """Percurso simulado em linha reta entre dois pontos de via (o desenho pelas ruas vem do OSRM no navegador)."""
    return [BASE, destino(tarefa_id)]


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
