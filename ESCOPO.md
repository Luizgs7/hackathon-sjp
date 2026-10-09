# Escopo do protótipo — Missões SJP

> Hackathon SJP — Soluções Inovadoras para a Gestão Pública (SIMOT / Prefeitura de São José dos Pinhais)
> Documento de escopo do que foi planejado, construído e validado até 09/10/2026.

---

## 1. Contexto

O manual do desafio (`../sdd/Manual_Hackathon_SJP_A5 (1).pdf`) pede um protótipo de **plataforma genérica de acompanhamento e execução de tarefas**, usando o Help Desk municipal como exemplo. O problema central: depois que o técnico sai com a ordem de serviço em papel, a gestão não sabe o que acontece até ele voltar.

A solução precisa ligar **quatro papéis**:
- **Atendente**: registra a demanda;
- **Gestor**: organiza e atribui;
- **Executor**: executa em campo;
- **Demandante**: confirma e avalia.

Além disso, ela precisa ter integração bidirecional com sistemas existentes, notificação push, gamificação, inteligência artificial e possibilidade de instalação no datacenter municipal (Lei 4.715/2025, arts. 52–55).

## 2. Entregas realizadas

| # | Entrega | Local |
|---|---|---|
| 1 | Plano completo de desenvolvimento (~48 h), com arquitetura, IA, gamificação, roteiro do item 17 e *Definition of Done* | `../sdd/plano-completo-48h.md` |
| 2 | Plano do protótipo simplificado (~1 h), com *Definition of Done* | `../sdd/plano-prototipo-1h.md` |
| 3 | **Protótipo funcional** (Parte B do plano), rodando localmente no navegador | `prototipo/` |
| 4 | Página de **autoatendimento do servidor com chat de IA** (incremento posterior) | `prototipo/` (`/servidor`) |
| 5 | Documentação de uso, requisitos, licenças e limitações | `prototipo/README.md` |
| 6 | Este documento de escopo | `prototipo/ESCOPO.md` |

## 3. Decisões tomadas

| Tema | Decisão | Motivo |
|---|---|---|
| Stack | Python (FastAPI) + SQLite + templates Jinja2 + htmx + Pico CSS | Escolha da equipe por backend Python. Máxima simplicidade: sem Docker, sem build de frontend, sem Node |
| Mobile | **Sem app nativo.** O executor usa uma página web mobile (`/campo`) com Web Push | Orientação da equipe. O Aspecto negociável 2 aceita "aplicação web mobile", e o Requisito 3 continua atendido |
| IA | Endpoint gratuito da NVIDIA (`nvidia/nemotron-3-super-120b-a12b`), compatível com OpenAI, **só para testes** | Escolha da equipe. Fica declarado como componente simulado: em produção, basta trocar `LLM_BASE_URL` para vLLM/Ollama no datacenter |
| Modo raciocínio do LLM | Desligado por padrão (`LLM_SEM_RACIOCINIO=true`) | O modelo gastava tokens "pensando" e devolvia resposta vazia. Sem raciocínio, a triagem caiu de ~13 s (com falha) para ~4 s |
| Fallback | Toda função de IA tem alternativa por regras/palavras-chave | A demo nunca trava se a IA ficar indisponível. A interface mostra a fonte (🤖 LLM / ⚙️ regras) |
| Abertura de chamado pelo servidor | O servidor envia uma **solicitação** (`SOL-xxxx`). O **atendente registra** o chamado | Requisito 1: "a abertura deverá ser realizada por atendente ou responsável autorizado" |
| Identidade visual | Paleta institucional #004668, #2094d2, #96c7e2 e #4d4f51. Vermelho só como alerta semântico | Pedido da equipe. Os estados sempre levam ícone + texto, sem depender só de cor (item 11) |
| Narrativa da gamificação | "Guardiões de SJP": tarefas são **missões**, com níveis Recruta → Explorador → Guardião → Lenda | Aspecto negociável 3 |

## 4. Funcionalidades construídas

### 4.1 Sistema legado simulado — SisChamados (`legado.py`, porta 8001)
- O atendente grava o chamado (formulário pré-preenchido com o caso do item 17) e o legado envia a demanda à plataforma pela API.
- Recebe de volta as atualizações (andamento, impedimento, conclusão, validação, reabertura) e mostra o histórico de cada chamado.
- O botão **"Simular sistema fora do ar"** demonstra a fila de sincronização.
- O botão **"Reenviar"** demonstra que não há duplicidade.

### 4.2 Atendente (`/atendente`)
- Cadastro direto de tarefa na web, para setores sem sistema legado (ex.: Manutenção Predial).
- Fila **📨 Solicitações dos servidores**:
  - mostra a origem (via assistente ou sem IA) e a conversa completa com a IA;
  - o atendente ajusta os campos e **registra o chamado**, ou encerra sem chamado informando o motivo.

### 4.3 Gestor (`/gestor`, `/tarefa/{id}`, `/config`)
- **Painel** atualizado a cada 3 s:
  - faixa destacada **⛔ Impedidas** no topo;
  - colunas por estado;
  - filtro por setor;
  - 12 indicadores com explicação de cálculo ao passar o mouse.
- **Detalhe da tarefa**:
  - triagem da IA para conferência, com formulário pré-preenchido e editável, e opções de rejeitar ou refazer a triagem;
  - atribuição com envio de push;
  - resolução de impedimento com registro de quem apoiou;
  - linha do tempo completa;
  - status da sincronização com o legado;
  - link da demandante;
  - pontos gerados.
- **Configuração**:
  - regras de pontuação (JSON editável);
  - temporadas;
  - setores, equipes e tipos de tarefa, com palavras-chave;
  - fila de saída (outbox) com reprocessamento;
  - descrição dos perfis.

### 4.4 Executor — web mobile (`/campo`, `/campo/{id}`)
- Cabeçalho com nível, pontos da temporada, pontos a confirmar e medalhas.
- Ativação de **Web Push** (VAPID gerado localmente), com notificação de teste. Se a página estiver aberta, um banner na tela também avisa de missão nova.
- **Um botão principal por estado**, com poucos toques:
  1. Aceitar e ir ao local;
  2. Cheguei — iniciar atendimento;
  3. Concluir missão (relato e foto pela câmera).
- **Reportar impedimento**: escolher o motivo, detalhar e anexar foto. Motivos externos pausam o relógio do SLA.
- **Copiloto de campo** (chat com IA) com procedimentos internos e casos resolvidos parecidos.

### 4.5 Demandante (`/validar/{token}`)
- Acesso sem login, por link único da tarefa (simula SMS/e-mail).
- Mostra o resumo do serviço em linguagem simples (gerado pela IA) e a foto da evidência.
- **✔ Resolvido** ou **✖ Ainda com problema**, nota de 1 a 5 estrelas e comentário (obrigatório quando há pendência). A pendência reabre a tarefa.

### 4.6 Servidor — autoatendimento com IA (`/servidor`, somente web)
- O **chat de IA fica em destaque**, com sugestões rápidas para começar ("Estou sem internet", "A impressora não imprime"…).
- O assistente dá orientações seguras para leigos: nunca pede senha, nunca orienta abrir equipamento elétrico e, diante de risco, manda afastar as pessoas e pedir atendimento.
- Ações durante a conversa:
  - **✔ Resolvido**: chamado evitado, contado no indicador do painel;
  - **📝 Não resolveu: abrir solicitação**: a IA preenche o formulário com a conversa;
  - **↺ Nova conversa**.
- **"Abrir solicitação sem o assistente"**: formulário direto, sem IA, sempre visível.
- Acompanhamento pelo protocolo (`/servidor/acompanhar/{token}`): aguardando atendente → chamado registrado → status da tarefa → confirmação e avaliação.

### 4.7 Ranking (`/ranking`)
- Ranking individual e por equipe, com filtro por temporada e setor.
- Medalhas: 🎯 Primeira missão · ⭐ Cinco estrelas · 🌐 Mestre das Redes · 🤝 Parceiro de equipe · 🔓 Destravador.
- Regras de pontuação exibidas de forma clara.

## 5. Inteligência artificial — aplicações no fluxo

| # | Aplicação | Quem usa | Entrada | Saída | Controle humano |
|---|---|---|---|---|---|
| 1 | **Triagem de chamados** | Gestor | Descrição, local, contato, tipos, executores (competência e carga), histórico do local | Tipo, categoria, resumo | Gestor aceita, edita ou rejeita |
| 2 | **Classificação de criticidade** | Gestor | Mesmos dados + critérios (serviço essencial, atendimento ao público) | P1–P4 com justificativa e complexidade 1–5 | Idem |
| 3 | **Informações faltantes** | Gestor / atendente / executor | Dados do chamado vs. o que o tipo exige | Perguntas a confirmar | Informativo |
| 4 | **Sugestão de executor** | Gestor | Competências, carga, casos resolvidos | Executor + motivo | Pré-seleção editável |
| 5 | **Sugestão de apoio no impedimento** | Gestor | Motivo, detalhe, equipes, recorrência do motivo | Equipe e providência + alerta de recorrência | Informativo |
| 6 | **Copiloto de campo** (chatbot) | Executor | Tarefa, base de conhecimento (PR-TI-01/03/07/09, PR-MP-02/05), casos similares | Passo a passo com materiais e alertas de segurança | Consultivo |
| 7 | **Resumo para a demandante** | Demandante | Relato técnico do executor | Texto em linguagem simples | Relato original preservado |
| 8 | **Assistente de autoatendimento** (chatbot) | Servidor | Conversa + orientações seguras para leigos | Orientação para resolver sozinho | Servidor decide se resolveu |
| 9 | **Estruturação da solicitação** | Servidor → atendente | Conversa completa | Assunto, descrição, local, tentativas, setor | Servidor revisa; atendente confere e registra |

Toda sugestão que altera prioridade, encaminhamento ou avaliação passa por conferência humana, como pede o item 10 do manual. Cada decisão fica registrada na linha do tempo (sugestão aceita, editada ou rejeitada).

## 6. Atendimento aos 7 requisitos não negociáveis

| Requisito | Como foi atendido |
|---|---|
| **R1** Aplicação genérica e cadastro autorizado | Setores, equipes e tipos configuráveis. Dois setores de exemplo (TI – Help Desk e Manutenção Predial). Cadastro web restrito a atendente/gestor. Solicitações de servidores **só viram chamado após registro do atendente** |
| **R2** Integração bidirecional | Entrada `POST /api/integracao/{origem}/tarefas` com chave de API, **idempotente** por `(origem, external_id)`. Saída por *transactional outbox* (mesma transação da mudança de estado), entregue em ordem, com reenvio automático a cada 5 s e número de sequência para o legado ignorar repetições. Cada tarefa mantém a referência ao chamado de origem (ex.: `LG-2031`) |
| **R3** Web + mobile com push | Gestão em `/gestor` (web) e execução em `/campo` (web mobile). Web Push via Service Worker e VAPID gerado localmente. Push (avisa o executor) e integração (sincroniza registros) são mecanismos separados |
| **R4** Rastreabilidade, validação e acesso | Tabela de eventos imutável (quem, papel, quando, de→para, texto, anexo). Fotos de evidência e de impedimento. Validação, avaliação e pendência pela demandante. **Conclusão do executor ≠ resolução confirmada.** Permissões por papel; executor só acessa as próprias missões (retorna 403) |
| **R5** Gamificação configurável | Ledger de pontos por temporada. Regras editáveis. Ranking individual e por equipe, por setor e período. **Encerrar temporada zera o placar e preserva o histórico** |
| **R6** IA demonstrável | Nove aplicações (seção 5), com fallback. Componente externo declarado como simulado, com caminho on-premise documentado |
| **R7** Hospedagem municipal e código-fonte | Python + SQLite, sem serviço de nuvem obrigatório. htmx e Pico CSS ficam em arquivos locais (sem CDN). `run.sh` instala e executa. Dependências com versões fixas e licenças listadas no README |

## 7. Regras de gamificação implementadas

Todos os valores abaixo são configuráveis em `/config`:

- **Entrega:** 20 pts × complexidade (1–5), lançados como **provisórios** quando o executor conclui.
- **Qualidade:** na confirmação da demandante, os pontos viram **definitivos**, multiplicados pela nota (1★×0,5 · 2★×0,75 · 3★×1,0 · 4★×1,2 · 5★×1,5).
- **Prazo:** +20 se o tempo líquido ficou dentro do SLA da criticidade (P1 60 min · P2 4 h · P3 24 h · P4 72 h). **Impedimentos externos são descontados do tempo** e não penalizam.
- **Sem reabertura:** +15. Se houver pendência, os pontos provisórios ficam **suspensos** até a nova resolução.
- **Colaboração:** +15 para o colega que apoiou a missão; +10 para o gestor que resolveu o impedimento.
- **Temporadas:** reinício do placar (ex.: mensal) com histórico preservado. Recompensas apenas simbólicas (níveis e medalhas).

## 8. Indicadores do painel

Recebidas · em andamento · impedidas · aguardando validação · resolvidas · tempo médio até iniciar · tempo médio até confirmar · satisfação média (CSAT) · reaberturas · **resolvidos pelo assistente** · **solicitações a registrar** · sincronizações pendentes · principais motivos de impedimento.

Cada indicador explica, ao passar o mouse, como é calculado e de quais registros vem (eventos, impedimentos, validações, outbox, autoatendimento).

## 9. Arquitetura e arquivos

```
prototipo/
├── app.py                 Plataforma (porta 8000): domínio, banco, IA, integração, push, gamificação, rotas
├── legado.py              SisChamados Legado simulado (porta 8001), banco próprio
├── run.sh                 Cria venv, instala dependências, sobe os 2 serviços (--reset recria os dados)
├── requirements.txt       Dependências com versões fixas
├── .env.example           Configuração (LLM, integração, sessão, push). O .env real fica fora do git
├── README.md              Como rodar, roteiro da demo, requisitos, licenças, limitações
├── ESCOPO.md              Este documento
├── static/                htmx, Pico CSS (locais), app.css (paleta), sw.js (Service Worker do push)
└── templates/             login, atendente, gestor/_quadro, tarefa, campo/_campo_lista/missao/_chat,
                           validar, ranking, config, servidor/_servidor_chat/servidor_solicitar/servidor_acompanhar
```

**Tabelas principais:**
- cadastro e tarefas: `setores`, `equipes`, `tipos`, `usuarios`, `tarefas`, `eventos`, `impedimentos`;
- integração e push: `outbox`, `push_subs`;
- gamificação: `pontos`, `temporadas`, `config`;
- chat do executor: `chat`;
- autoatendimento do servidor: `autoatendimento`, `auto_msgs`, `solicitacoes`.

**Sessões:** os cookies do painel web e do campo são separados e assinados com HMAC. Assim, gestor e executor podem ficar lado a lado no mesmo navegador.

## 10. Validação realizada

Dois roteiros automatizados via HTTP rodaram contra os serviços reais, com a IA da NVIDIA ativa (sem navegador). **Todos os passos passaram.**

**Roteiro do item 17 (jornada principal):**
1. O legado registra `LG-2031`. O reenvio não duplica.
2. A IA faz a triagem em ~4 s: P2, "Rede / conectividade", informações faltantes, Rafael sugerido.
3. O gestor atribui.
4. O executor segue a caminho → inicia atendimento → consulta o copiloto.
5. O executor reporta impedimento "falta de material" com foto.
6. O gestor resolve com apoio do Bruno.
7. O executor conclui com foto (+60 provisórios).
8. A demandante confirma com 5★.
9. Pontos definitivos: 90 + bônus de SLA 20 + bônus sem reabertura 15; Bruno +15; Paula +10.
10. As 8 atualizações chegam ao legado. A medalha 🌐 aparece.
11. Variante: pendência → **Reaberta**.
12. Legado fora do ar: 6 atualizações ficam pendentes e sincronizam ao religar.
13. Cadastro web em Manutenção Predial; nova temporada com histórico preservado; executor bloqueado em missão alheia (403).

**Roteiro do autoatendimento:**
- Conversa resolvida pelo assistente, contabilizada no indicador.
- Conversa → rascunho da IA ("Sala de vacinação da UBS Vila Nova sem internet", com local e tentativas) → `SOL-0001` com a conversa anexada.
- Solicitação sem IA (`SOL-0002`).
- Registro bloqueado sem login.
- Fila do atendente → `SOL-0001` registrada como tarefa com triagem; `SOL-0002` encerrada sem chamado.
- O servidor acompanha a tarefa pelo protocolo.

**Não validado automaticamente:** a exibição real da notificação push no navegador (depende da permissão do usuário e do serviço de push do navegador) e a aparência visual das telas.

## 11. O que é funcional, simulado ou futuro

| Situação | Itens |
|---|---|
| **Funcional** | Toda a jornada do item 17, integração com o legado simulado, push, IA com fallback, autoatendimento, pontuação, ranking, temporadas, indicadores e configuração |
| **Simulado** | LLM externo da NVIDIA (produção: modelo open-weight no datacenter via vLLM/Ollama, trocando só `LLM_BASE_URL`) · sistema legado (`legado.py`) · login por escolha de usuário fictício · envio do link à demandante (aparece na tela no lugar de SMS/e-mail) · identificação do servidor no autoatendimento (dados digitados) |
| **Futuro** (plano de 48 h) | Autenticação integrada (LDAP/AD) · PostgreSQL e Docker Compose · controle de acesso aos arquivos enviados · fluxos de estado configuráveis por tipo · temporada mensal agendada · testes automatizados no repositório · push/app próprio para operar sem internet · busca semântica (RAG) na base de conhecimento · insights de impedimentos recorrentes com IA |

## 12. Como executar

```bash
cd prototipo
cp .env.example .env     # preencher LLM_API_KEY (sem chave, a IA usa regras)
./run.sh                 # ou ./run.sh --reset para recomeçar com os dados fictícios
```

- Plataforma: http://localhost:8000
- Autoatendimento do servidor: http://localhost:8000/servidor
- Legado simulado: http://localhost:8001
- API (OpenAPI): http://localhost:8000/docs

**Usuários fictícios:**
- Carlos Lima (atendente)
- Paula Mendes (gestora + atendente)
- Rafael Costa e Diego Santos (Infra de Redes)
- Bruno Alves (Suporte e Almoxarifado TI)
- Juliana Rocha (Manutenção Predial)
- Ana Souza (demandante, via link)

## 13. Pontos de atenção

- **Chave da API de IA:** fica em `prototipo/.env`, que está no `.gitignore`. Como ela foi compartilhada em conversa, recomenda-se gerar uma nova antes de publicar ou apresentar.
- **Soberania digital:** com o endpoint externo, use somente dados fictícios. A implantação definitiva deve apontar para um LLM hospedado no datacenter municipal.
- **Web Push:** depende do serviço de push do navegador (Google/Mozilla). O conteúdo vai cifrado e sem dados pessoais. A página aberta mostra um aviso na tela como contingência.
