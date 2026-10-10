# ProtÃ³tipo pÃºblico na Vercel

O usuÃ¡rio autorizou publicar o protÃ³tipo na Vercel com JEV e Haiku, aceitando registros descartÃ¡veis. NÃ£o usar dados municipais reais.

`python tools/prepare_vercel_demo.py` monta `.tools/vercel-demo` com uma lista explÃ­cita de fontes e assets. NÃ£o copia `.env`, bancos locais, anexos, chaves VAPID, histÃ³rico Git ou logs. O aplicativo local nÃ£o Ã© alterado por essa preparaÃ§Ã£o.

O entrypoint `tools/vercel_demo.py` usa SQLite e uploads no diretÃ³rio temporÃ¡rio da funÃ§Ã£o. Registros e sessÃµes de acesso podem desaparecer quando a instÃ¢ncia for reciclada; instÃ¢ncias diferentes nÃ£o compartilham o banco. Essa publicaÃ§Ã£o Ã© para demonstraÃ§Ã£o, sem garantia de persistÃªncia.

O legado simulado funciona em `/legado/`, com integraÃ§Ã£o interna e os mesmos contratos da API. OperaÃ§Ãµes antes executadas em threads passam a terminar dentro da requisiÃ§Ã£o; nÃ£o dependem de um worker permanente. Push externo estÃ¡ desativado. HÃ¡ um limite simples de 20 POSTs por minuto por origem e instÃ¢ncia, que nÃ£o substitui proteÃ§Ã£o ou cotas de produÃ§Ã£o.

Configurar `BLOB_READ_WRITE_TOKEN`, `TYPESAFE_API_KEY`, `ANTHROPIC_API_KEY`, `SESSION_SECRET` e os modelos como variÃ¡veis privadas de execuÃ§Ã£o da Vercel. Nunca colocar valores neste documento, no Git ou no frontend. O fallback por regras continua disponÃ­vel quando a IA nÃ£o responde.

Publicar somente o pacote gerado, em um projeto prÃ³prio chamado `dataforge-prototipo`. O endereÃ§o pÃºblico deve ser verificado sem sessÃ£o Vercel antes de ser entregue. Nenhuma compra de crÃ©ditos ou mudanÃ§a para plano pago estÃ¡ autorizada.

## PublicaÃ§Ã£o verificada em 10 de outubro de 2026

EndereÃ§o: https://dataforge-prototipo.vercel.app/login. Projeto: joaoezaus-projects/dataforge-prototipo. VariÃ¡veis sensÃ­veis de produÃ§Ã£o configuradas no projeto. PublicaÃ§Ã£o via CLI, sem push ao GitHub. Login, assets, perfis, fragmentos e legado responderam sem autenticaÃ§Ã£o Vercel. Os dois caminhos completos de atendimento foram validados com dados fictÃ­cios; Haiku respondeu com HTTP 200. As tarefas 156 e 157 foram criadas apenas para verificar o protÃ³tipo publicado.

Para atualizar: gerar o pacote e executar `vercel deploy --prod --yes` dentro de `.tools/vercel-demo`, com o CLI autorizado. NÃ£o executar a publicaÃ§Ã£o da raiz do repositÃ³rio. O pyproject gerado declara explicitamente as dependÃªncias de requirements.txt.

## Login, Clarity e continuidade do chat

Clarity yvjgde7unl incluÃ­do nas bases DataForge e legado a pedido do usuÃ¡rio. A faixa pÃºblica foi removida, mantendo noindex e controles do protÃ³tipo. Login com quatro perfis em um Ãºnico dropdown.

O histÃ³rico do chat acompanha cada POST em um snapshot comprimido, com assinatura HMAC e validade de um dia. InstÃ¢ncias sem as mensagens anteriores podem recuperÃ¡-las desse snapshot. Um cookie HttpOnly, limitado a 3800 caracteres, permite recuperar conversas pequenas em recargas e no formulÃ¡rio da solicitaÃ§Ã£o; acima disso a continuidade vale para os envios na pÃ¡gina aberta. O limite de descompressÃ£o Ã© 200 KB. A assinatura usa SESSION_SECRET privada e estÃ¡vel do projeto. Nova conversa, resoluÃ§Ã£o e solicitaÃ§Ã£o limpam o cookie. Essa recuperaÃ§Ã£o nÃ£o oferece persistÃªncia compartilhada para chamados e sessÃµes de acesso.

Conversas resolvidas sem OS sÃ£o arquivadas no localStorage do navegador como snapshots assinados. POST /servidor/historico apenas verifica e exibe, sem reinserir dados nem contabilizar. ResoluÃ§Ã£o restaura o snapshot ativo se necessÃ¡rio, marca a conversa resolvida uma vez e exibe a versÃ£o arquivada. O indicador de resoluÃ§Ã£o com IA exclui conversas com resposta de fallback; o total geral e a diferenÃ§a permanecem visÃ­veis. Os contadores sÃ£o temporÃ¡rios por instÃ¢ncia; o histÃ³rico pessoal depende dos dados do navegador. Limites de tamanho dos snapshots ativos continuam vÃ¡lidos; os encerrados podem ser consultados apÃ³s sua expiraÃ§Ã£o ativa, pois nÃ£o podem retomar o chat ou alterar contadores.

## Portal do solicitante e acompanhamento no chat

Dashboard em /servidor/dashboard lista somente registros do e-mail autorizado, incluindo conversas iniciadas/resolvidas e solicitações registradas em chamados. Abrir uma conversa pelo dashboard verifica a relação conversa_acessos. Solicitações via formulário também iniciam uma conversa de acompanhamento. A consulta no chat lê o status real, sem usar IA para inventar o andamento ou criar outra solicitação. A resolução sem chamado é recusada se já houver solicitação ou se a mensagem for fora de escopo. JEV distingue assuntos fora do Helpdesk; receitas culinárias conhecidas são recusadas antes de chamadas externas. A camada HTTP preserva todos os Set-Cookie; a identidade fictícia assinada da Ana pode recuperar somente seu próprio token ausente, respeitando expiração.
