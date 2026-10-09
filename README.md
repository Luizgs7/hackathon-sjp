# 🛡️ Missões SJP — protótipo (Hackathon SJP / SIMOT)

Plataforma genérica de acompanhamento e execução de tarefas, com gamificação ("Guardiões de SJP") e IA.
O Help Desk de TI é o exemplo. A mesma estrutura atende Manutenção Predial, vistorias e outros setores.
Plano de referência: [`../sdd/plano-prototipo-1h.md`](../sdd/plano-prototipo-1h.md).

## Como rodar

Requisitos: Python 3.11+ (testado com 3.13) e acesso à internet só para a IA de testes e para o push do navegador.

```bash
cd prototipo
cp .env.example .env        # preencha LLM_API_KEY (sem chave, a IA usa o fallback por regras)
./run.sh                    # instala dependências e sobe os 2 serviços
./run.sh --reset            # recomeça do zero com os dados fictícios
```

| Endereço | O quê |
|---|---|
| http://localhost:8000 | Plataforma Missões SJP (atendente, gestor, executor, demandante) |
| http://localhost:8001 | **SisChamados Legado** (sistema existente simulado) |
| http://localhost:8000/servidor | **Autoatendimento do servidor**: assistente de IA antes de ligar para o Help Desk |
| http://localhost:8000/docs | Documentação OpenAPI da API de integração |

### Autoatendimento do servidor (`/servidor`, só web)
O servidor com um problema conversa primeiro com o **Assistente Virtual**, que fica em destaque na página. O assistente:
- dá orientações seguras para leigos (nunca pede senha nem manda abrir equipamento elétrico);
- se resolver, o servidor clica em **✔ Resolvido** e nenhum chamado é aberto. Isso vira o indicador "resolvidos pelo assistente" no painel;
- se não resolver, **Abrir solicitação** faz a IA preencher o formulário a partir da conversa, e o servidor revisa antes de enviar.

Há também o botão **Abrir solicitação sem o assistente**, com formulário direto, sem IA.

Para cumprir o **Requisito 1** (a abertura é feita por atendente ou responsável autorizado), o servidor envia uma *solicitação* com protocolo `SOL-xxxx`. Ela cai na fila **📨 Solicitações dos servidores** da tela do atendente, que confere, ajusta e **registra o chamado** (ou encerra sem chamado). O servidor acompanha tudo pelo link do protocolo, inclusive a confirmação final da resolução.

O painel web (atendente/gestor) e o campo (executor) usam **sessões separadas**. Assim dá para abrir a Paula (gestora) numa aba e o Rafael (técnico) em outra, no mesmo navegador. Para o executor, use o DevTools em modo celular (Ctrl+Shift+M).

## Roteiro de demonstração (item 17 do manual)

1. **Legado** (`:8001`): Carlos grava o chamado "Ponto de rede da sala de vacinação não funciona – UBS Vila Nova" (o formulário já vem preenchido). O legado envia o chamado à plataforma pela API.
2. **Gestor** (`:8000` → Paula Mendes): o card aparece no painel com 🔗 `LG-2031`. Em segundos, a **IA** sugere:
   - tipo e criticidade **P2**, com justificativa;
   - informações faltantes;
   - executor sugerido (Rafael), com o motivo.
3. Paula abre o card, **confere** os campos (pré-preenchidos pela IA e editáveis) e clica em **Atribuir missão**.
4. **Executor** (outra aba → Rafael Costa → *Ativar notificações*): recebe o **push**, toca em **Aceitar e ir ao local** e depois em **Cheguei — iniciar atendimento**.
5. Rafael pergunta ao **Copiloto de campo**: "o ponto está sem link e o LED do switch apagado". A IA responde com o passo a passo do procedimento PR-TI-07.
6. Ele reporta o **impedimento** "Falta de material", com foto. O relógio do SLA pausa.
7. Paula vê o card na faixa **⛔ Impedidas**, lê a sugestão da IA ("acionar Almoxarifado"), registra a providência com **apoio do Bruno** e libera o executor.
8. Rafael **conclui** com relato e foto e ganha **pontos provisórios**. A IA reescreve o relato para a demandante.
9. **Demandante**: na tarefa, Paula clica em *Abrir link da demandante*. Ana marca **✔ Resolvido** e dá ★★★★★.
10. O **legado** mostra todo o histórico e "Resolvida – confirmada". O **ranking** mostra os pontos definitivos e as medalhas, e os **indicadores** são atualizados.

**Variantes:**
- ✖ *Ainda com problema*: a tarefa volta como 🔁 **Reaberta** no painel e os pontos ficam suspensos.
- No legado, **Simular sistema fora do ar**: as atualizações ficam ⏳ pendentes (veja em *Configurar*) e sincronizam ao religar.
- **Reenviar** um chamado no legado não cria duplicata.
- Cadastro direto em *Atendimento* para **Manutenção Predial** (setor sem legado).
- *Configurar*: regras de pontuação, setores, equipes, tipos e **nova temporada** (placar zera, histórico preservado).

## Requisitos inegociáveis: onde estão

| Requisito | Implementação |
|---|---|
| R1 Genérico / cadastro autorizado | Setores, equipes e tipos configuráveis em `/config`. Cadastro web em `/atendente` (só atendente/gestor) |
| R2 Integração bidirecional | Entrada `POST /api/integracao/{origem}/tarefas` (API key, idempotente por `origem+external_id`). Saída por *transactional outbox* com reenvio automático a cada 5 s, em ordem |
| R3 Web + mobile + push | `/gestor` (web) e `/campo` (web mobile). Web Push com VAPID gerado localmente (`sw.js`) e aviso na tela como contingência |
| R4 Rastreabilidade e acesso | Tabela `eventos` (quem/quando/de→para/texto/anexo), evidências em foto, validação por token, permissões por papel |
| R5 Gamificação configurável | Ledger `pontos` com temporada. Regras em JSON editável. Ranking individual e por equipe, por setor e temporada |
| R6 IA | Triagem (tipo, criticidade, faltantes, executor) com aceite/edição/rejeição, copiloto de campo, sugestão de apoio no impedimento e resumo para a demandante. Fallback por regras |
| R7 Hospedagem municipal | Python + SQLite, sem serviços de nuvem obrigatórios. Código-fonte completo e dependências listadas abaixo |

## Funcional × simulado × futuro

- **Funcional:** toda a jornada acima, a integração com o legado simulado, o push, a pontuação e os indicadores.
- **Simulado:**
  - **IA**: usa o endpoint gratuito da NVIDIA (externo), só com dados fictícios. Em produção, troca-se o `LLM_BASE_URL` para **vLLM/Ollama no datacenter municipal**, com um modelo open-weight (ex.: Qwen2.5-14B-Instruct), sem mudar o código. A interface identifica a fonte (🤖 LLM / ⚙️ regras).
  - **Sistema legado:** é o `legado.py`.
  - **Login:** seleção de usuário fictício.
  - **Envio do link à demandante:** exibido na tela no lugar de SMS/e-mail.
- **Limitações e próximos passos** (ver [`../sdd/plano-completo-48h.md`](../sdd/plano-completo-48h.md)):
  - autenticação real (LDAP/AD municipal);
  - PostgreSQL;
  - Docker Compose;
  - controle de acesso aos arquivos de `uploads/`;
  - fluxos de estado configuráveis por tipo;
  - temporada mensal agendada;
  - testes automatizados;
  - app/gateway de push próprio para operar sem internet: o Web Push passa pelo serviço do navegador (Google/Mozilla), mas o conteúdo vai cifrado e sem dados pessoais.

## Componentes de terceiros (todos gratuitos)

| Componente | Licença | Uso |
|---|---|---|
| FastAPI, Uvicorn, Starlette | MIT / BSD-3 | servidor web |
| Jinja2, python-multipart, python-dotenv | BSD-3 / Apache-2.0 / BSD-3 | templates, formulários, configuração |
| httpx | BSD-3 | integração HTTP |
| openai (SDK) | Apache-2.0 | cliente do LLM (protocolo compatível; não exige a OpenAI) |
| pywebpush, py-vapid | MPL-2.0 | Web Push |
| htmx 2.0.4, Pico CSS 2.0.6 | 0BSD / MIT | interface (arquivos locais em `static/`, sem CDN) |
| SQLite | domínio público | banco de dados |
| Nemotron 3 Super (NVIDIA API) | termos NVIDIA, gratuito para testes | **somente protótipo**; substituído por modelo local em produção |
