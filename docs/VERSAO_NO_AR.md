# Versão no ar do Help Desk SJP (10 de outubro de 2026)

Protótipo descartável para o hackathon: **dados fictícios**, sem integração com sistemas reais da prefeitura.

- **Site:** https://dataforge-prototipo.vercel.app (entrar em `/login`, sem senha: escolha o perfil em "Tipo de usuário").
- **Código desta versão:** este branch (`demo-no-ar-2026-10-10`), commit indicado no topo do histórico.
- **Testes:** `129` testes automatizados passando (`python -m unittest discover -s tests`).

## Perfis de teste (1 solicitante, 1 atendente, 1 gestora do Help Desk, 3 gestores técnicos, 3 técnicos)

| Perfil no login | Quem | O que faz |
|---|---|---|
| Solicitante | Ana Souza | Conversa com o assistente de IA, fala com atendente, abre e acompanha chamados, confirma ou reabre |
| Helpdesk | Carlos Lima | Assume conversas, registra chamados, resolve no 1º nível, encaminha ao gestor técnico |
| Gestor do atendimento | Paula Mendes | Painel (kanban) de todos os chamados, métricas, configuração e pessoas; encaminha ao gestor técnico |
| Gestor Suporte Técnico | Roberto Nunes | Aloca o técnico da área; vê só chamados da sua área |
| Gestor Telecom | Helena Prado | idem |
| Gestor Telefonia | Camila Duarte | idem |
| Técnico Suporte Técnico | Diego Santos | Missões no celular, rota, execução |
| Técnico Telecom | Rafael Costa | idem |
| Técnico Telefonia | Marcos Vieira | idem |

Cada técnico já vem com missões em vários estágios (aguardando, a caminho, executado aguardando confirmação) e cada área tem um chamado devolvido para o gestor técnico reavaliar.

## Fluxo completo (única forma de alocar um técnico)

1. **Solicitante** descreve o problema ao assistente. Se resolver, não abre chamado. Se não, **fala com o atendente** (mesmo chat) ou **abre chamado**.
2. **Atendente** assume a conversa, responde, e registra o chamado. Pode **resolver no atendimento** (1º nível) ou **encaminhar ao gestor técnico**.
3. **Gestor do atendimento ou atendente** escolhe **1 dos 3 gestores técnicos** (a área vem do gestor escolhido). O chamado sai da fila do atendimento e aparece com o selo "aguardando gestor técnico".
4. **Gestor técnico** da área recebe aviso e **aloca o técnico** (tipo, criticidade, orientação). Só ele aloca; atendente e gestora do Help Desk não escolhem técnico.
5. **Técnico** (celular): aceitar, a caminho, em execução, impedimento, devolver ao gestor técnico ou concluir com relato e foto.
6. **Solicitante** confirma (nota 1 a 5) ou reabre o chamado. Reaberto volta à fila do atendimento.

Avisos (sino) chegam a cada perfil conforme a etapa, inclusive ao gestor técnico da área em cada transição.

## O que mudou nesta versão

- **Kanban com arrastar e soltar** (gestora e gestores técnicos): soltar o cartão abre um modal de confirmação; quem muda o status continua sendo o endpoint existente (as regras de transição valem sempre). Botão "Mover…" como alternativa por teclado e celular.
- **Página Equipe** (carga por técnico, mapa, histórico de rotas, km rodados) é do **gestor técnico**; a gestora do Help Desk não a vê.
- **Gamificação** (ranking, pontuação, níveis) só para técnicos e gestores técnicos. Níveis: Iniciante, Intermediário (100), Avançado (300), Especialista (600 pontos).
- **Rota do técnico** desenhada pelas ruas (OSRM/OpenStreetMap, no navegador); destinos simulados em vias reais de São José dos Pinhais.
- **Tema claro e escuro** em todas as telas, inclusive o mapa.
- **Resiliência do site:** se o armazenamento compartilhado (Vercel Blob) falhar, o site continua no ar com o banco local da instância, sem 503.

## Limitações conhecidas

- Posições, distâncias e rotas são **simuladas** a partir do texto do local (não há GPS).
- Os números do canvas (10.089 chamados, 63,6%, 90 h) não foram reconciliados com a base (9.969 chaves únicas); não tratar como verificados.
- A página **Equipe de atendimento** da gestora do Help Desk ainda **não existe** (pedida, não construída).
- A gestora do Help Desk cadastra e ativa/inativa pessoas em Configurar; o gestor técnico não consegue inativar o técnico dele.
- O arrastar do kanban foi testado só em desktop; no celular vale o botão "Mover…".
- Push em celular real (iPhone exige instalar o app) não foi testado.
- O Vercel Blob retornou 403 (provável cota); sem ele os dados deixam de ser compartilhados entre instâncias.

## Como rodar localmente

```bash
cp .env.example .env     # chaves de IA são opcionais; sem elas há fallback local
./run.sh --reset         # instala dependências e sobe a plataforma (8000) e o legado (8001) com dados fictícios
python -m unittest discover -s tests
```

Deploy da demonstração: `powershell -ExecutionPolicy Bypass -File .\publicar.ps1` (testa, prepara o pacote, publica na Vercel e verifica o site). Detalhes em `DEPLOY_VERCEL.md`.

## Onde está cada coisa

- `app.py`: backend (rotas, regras, notificações, quadro). `rastro.py`: posições e rotas simuladas.
- `templates/df/`: telas; `static/ui/`: CSS e JS (`quadro_dnd.js` = arrastar e soltar; `rota.js`/`rota_ruas.js` = mapa e rota).
- `tools/vercel_demo.py`: entrada da demonstração na Vercel; `tools/demo_store.py`: banco compartilhado (Blob).
- `tests/test_fluxos_integrados.py`: jornada completa nas 3 áreas; `tests/test_kanban_dnd.py`: arrastar e soltar.
