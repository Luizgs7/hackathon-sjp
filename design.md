# DataForge — biblioteca web

Fundação frontend na branch `ux-ui-frontend-design-jev`. FastAPI, Jinja2 e htmx continuam sendo a stack do produto. A biblioteca está isolada e **não é carregada pelas telas atuais**, que mantêm Pico CSS, Inter e Lucide. Este documento descreve a biblioteca; sua existência não aprova funcionalidades novas para o produto.

## Fonte e rastreabilidade

- [Figma DataForge / Componentes](https://www.figma.com/design/uJ1MNIY2FcYyJH98nSDCRd/DataForge?node-id=2118-12): fonte das famílias, estados, variantes e fundamentos.
- [FigJam DataForge](https://www.figma.com/board/7MTk0Qfzr0SAQyEpdyS2Iy/DataForge?node-id=40000016-211): contexto de oportunidade; consultar os documentos do projeto antes de alterar escopo.
- `frontend/figma/collections.json`: IDs, nomes e modos das coleções.
- `frontend/figma/variables.json`: **400 variáveis**, com IDs e aliases originais preservados.
- `frontend/figma/text-styles.json`: **39 estilos Geist**, incluindo métricas e IDs.
- `frontend/figma/components.json`: **164 entradas e 520 variantes**; IDs, propriedades e geometria de referência.
- `frontend/figma/design-context.json`: referências detalhadas obtidas por leitura do Figma MCP para as **156 entradas implementáveis**. Código retornado é referência de projeto, não uma dependência React nem um asset de runtime.
- `frontend/components.manifest.json`: classificação, macro correspondente, variantes e exclusões. Não editar arquivos gerados; corrigir a fonte ou o gerador e compilar novamente.

Snapshot capturado em 2026-10-09. Não há sincronização automática com alterações futuras no Figma. Esta rodada não escreve no arquivo Figma. A referência detalhada teve interrupções pelo limite do MCP; as consultas pendentes foram retomadas e capturadas. A marca `reference-captured` significa referência disponível, **não uma certificação de equivalência pixel a pixel**.

## Direção do produto

**Servidor → Helpdesk** é a entrada do atendimento. A IA pode orientar demandas simples e sugerir dados de triagem. O Helpdesk confere as informações e resolve no atendimento ou encaminha à equipe técnica, conforme o fluxo aprovado. Conclusão técnica exige validação do servidor para o fechamento definitivo.

Estados oficiais: **Novo → Encaminhado → Executado → Concluído**, além de **Devolvido**, **Reaberto** e **Cancelado**. A caminho, em execução e impedido são etapas de campo enquanto o chamado está Encaminhado; não substituem os estados oficiais. Os rótulos de exemplo do Figma não devem reescrever esse contrato.

Usar a identificação genérica **IA** na interface. JEV e Haiku são detalhes da implementação, documentados no backend. A biblioteca não consulta esses serviços. Não apresentar a variante Municipal do painel Sobre a IA como infraestrutura já implantada: é uma apresentação para uma integração futura confirmada.

Gamificação, planejamento, foco, mapas, câmera, localização e push têm componentes disponíveis; isso não os torna automaticamente parte do MVP. Usar dados fictícios no catálogo e nunca transformar cliques de demonstração em chamados reais.

## Tokens e temas

`python tools/frontend.py build` resolve aliases recursivamente, detecta ciclos e gera `frontend/tokens.generated.css` a partir do snapshot. Não duplicar cores da marca em CSS ou templates.

São exportadas variáveis CSS para cores primitivas, Brand Colors, Brand Semantic, espaçamento, raios e dimensões. Device Sizes permanece no JSON como referência de documentação; não impõe uma largura fixa ao navegador. Os nove contextos de Icon Color são exportados como `[data-icon-color]`: default, onprimary, muted, accent, danger, disabled, streak, gold e success.

O tema Tailwind é gerado do mesmo snapshot. Utilities têm prefixo **`df:`**, por exemplo `df:flex`, `df:gap-2`, `df:rounded-xl`. São compiladas dentro de `.df-ui`. Não há Preflight nem reset global. Regras próprias também começam por `.df-ui`; as declarações de fontes e keyframes são os únicos fundamentos compartilhados pelo CSS novo.

| Raio Figma | Valor |
| --- | ---: |
| none | 0 px |
| sm | 2 px |
| padrão | 4 px |
| md | 6 px |
| lg | 8 px |
| xl | 12 px |
| 2xl | 16 px |
| 3xl | 24 px |
| full | 9999 px |

Essa escala difere dos padrões do Tailwind. A unidade de espaçamento vem de Spacing/1 do snapshot, e não de uma segunda tabela manual. Estilos tipográficos gerados usam classes `df-type-*` e preservam família, tamanho, peso, entrelinha e tracking.

Tema explícito: `<section class="df-ui" data-theme="light">` ou `dark`. O catálogo oferece Claro, Escuro e Sistema, acompanha `prefers-color-scheme` e salva a preferência localmente. Botões Destructive usam Danger/Surface, Danger/Surface Hover e Text/On Danger em ambos os temas. Border/Input identifica os limites de campos. Esses quatro tokens refletem as correções do Figma registradas em `frontend/figma/corrections-2026-10-09.json`. A avaliação por estrelas ganha indicador adicional para não depender somente do dourado.

## Fontes e ícones locais

Geist Regular (400), Geist SemiBold (600) e Geist Mono Regular (400), conforme os estilos capturados. Não há Geist Pixel. Fontes WOFF2 e licença original OFL estão em `static/ui/fonts/`. Não solicitar fontes ao Google nem a CDNs em runtime.

Os 97 ícones Phosphor e sua licença MIT estão em `static/ui/icons/`; `static/ui/phosphor.svg` é o sprite gerado. Ícones de contorno e preenchidos preservam os nomes da origem. `ui/_renderer.html` expõe `icon(name)` para reutilização. Ícones decorativos têm `aria-hidden`; botões somente com ícone exigem label compreensível.

Origens fixadas em commits oficiais de Geist e Phosphor. `frontend/assets.lock.json` registra URLs e SHA-256 dos assets e do compilador instalado. Instalações seguintes verificam o lock e recusam substituição silenciosa. Nenhuma chave de API integra esses arquivos.

## Componentes Jinja2

O manifesto contém **133 componentes públicos**, **23 subcomponentes internos** e **8 referências de plataforma**. Os **156 componentes web** aparecem no catálogo, em **499 exemplos de variantes e propriedades**. Famílias com Platform=Web usam essa variante; outras variantes nativas dessa família são excluídas explicitamente no manifesto. Controles portáveis usam comportamento web acessível.

Macros são agrupadas em `templates/ui/`: ações, seleção, formulários, dados e conteúdo, diálogos, conversa, identidade, menus, mídia, execução, mapas, navegação, feedback, planejamento, foco, constância, gamificação, atividade, gráficos, avaliação e quadro/prioridade. Os nomes e arquivos exatos ficam no campo `implementation` do manifesto. O prefixo `.` do Figma identifica internos, que compartilham o renderer e as mesmas bases de composição.

Todos os wrappers têm o contrato:

```jinja2
{% import 'ui/01-acoes.html' as actions with context %}
{{ actions.button('resolver', label='Resolver no atendimento',
                  props={'Style': 'Primary', 'Size': 'Medium', 'State': 'Default'}) }}
```

Parâmetros: `id` único, `label`, `value`, `items`, `props`, `disabled`, `error`, `helper`, `href` e `media`. As propriedades mantêm os nomes do Figma, como State, Style, Size, Tone, Priority, Checked, Selected, Active e Platform. IDs devem ser únicos por instância, inclusive após swaps htmx. Textos e atributos são escapados pelo Jinja2; não aplicar `safe` a conteúdo fornecido por usuários. Configurar autoescape no ambiente consumidor. Não são necessários filtros Jinja customizados para usar os componentes.

Button e Icon Button aceitam `Loading`, `Focus Visible` e `Pressed`; carregamento conserva a largura e o nome acessível e bloqueia a ação. KPI Card recebe `Trend` e `Sentiment` separadamente: direção não determina se o resultado é favorável. Toast Danger oferece mensagem de falha e ação de tentar novamente.

Por padrão, os macros buscam assets em `/static/ui`. O catálogo define `ui_asset_base='assets'` em seu próprio ambiente. Aplicações montadas em subpaths poderão fornecer esse prefixo sem alterar o renderer. Os macros não carregam CSS ou JS por conta própria.

Gráficos recebem `items=[{'label': 'Novo', 'value': 20, 'secondary': 12}, ...]`. Bar aceita Single/Stacked, Line aceita Line/Area/Compare; Donut normaliza os valores das categorias e aceita Segments=3/4. `SeriesLabel` e `SecondaryLabel` nomeiam as séries. Os gráficos são SVG calculados a partir dos dados, com título, descrição, legenda e tabela textual; dados negativos são limitados a zero nessa visualização de contagem. Empty e Loading têm apresentação própria. Não usar os valores ilustrativos como indicadores reais.

Imagens e áudio são fornecidos por `media`; o estado sem mídia mostra uma apresentação vazia. Mapas e câmera têm apresentação e eventos locais de integração. Não há provedor de mapas, acesso à câmera, leitura de GPS, permissões reais de push ou contratação de serviços nesta biblioteca.

Exclusões integrais: Date Wheel, Time Wheel, Date Dialog, Status Bar, Home Indicator, Android Navigation, Browser Chrome e Screen. São rodas nativas, barras de sistema e molduras de dispositivo. Datas e horários portáveis usam controles do navegador; Switch, Navigation Bar e Action Sheet têm adaptações web documentadas em `web_adaptation`, sem emular APIs de sistema operacional.

## Comportamento e acessibilidade

`static/ui/library.js` usa delegação de eventos e um guard de carregamento único. `DataForgeUI.init()` pode ser chamado repetidamente. O listener `htmx:afterSwap` inicializa componentes recém-inseridos; antes/depois do swap, o foco e a seleção de texto são preservados quando um controle com o mesmo ID substitui o anterior.

- Inputs têm labels, ajuda e associação de mensagens de erro; checkbox suporta indeterminate.
- Abas usam tablist/tab/tabpanel e navegação por setas, Home e End.
- Menus aceitam teclado, Escape, fechamento externo e retorno ao summary.
- Dialog, modal, drawer e sheet usam `<dialog>` nativo: foco fica dentro da sobreposição e retorna ao acionador quando ela fecha.
- Seleção usa atributos ARIA, checkbox/radio nativos e grupos de escolha exclusiva.
- Tooltip aparece por foco/hover e pode ser dispensado com Escape.
- Controles têm foco visível; animações respeitam movimento reduzido.
- JSON Editor valida sintaxe localmente e impede salvar JSON inválido. A ação de salvar emite um evento; nenhuma persistência é realizada pela biblioteca.

O evento **`df:action`** propaga `{action, id, value?}`. Ações incluem toggle, change, select, tab, page, range, message, dialog-close, json-save, timer-complete, location, marker, capture e push-permission/push-help/push-test. O consumidor decide como tratar esses eventos e deve verificar dados/permissões no servidor. Não vincular automaticamente a endpoints do produto. Formulários de chat no catálogo são interceptados e não submetem requisições.

```javascript
document.addEventListener('df:action', event => {
  // Integrar somente em uma etapa aprovada de migração.
  const { action, id, value } = event.detail;
});
```

## Instalação, build e catálogo

Python 3.11+ e os requirements existentes. Tailwind **4.3.3 standalone** é baixado da release oficial e instalado em `.tools/`, fora do Git. A instalação requer internet; build com o compilador instalado e execução do catálogo não requerem Node nem internet. CSS compilado e assets locais são versionados em `static/ui/`.

Windows:

```powershell
.\tools\frontend.ps1 install
.\tools\frontend.ps1 build
.\tools\frontend.ps1 serve
# Outra janela, durante edição:
.\tools\frontend.ps1 watch
.\tools\frontend.ps1 check
```

Linux:

```bash
bash tools/frontend.sh install
bash tools/frontend.sh build
bash tools/frontend.sh serve
bash tools/frontend.sh watch
bash tools/frontend.sh check
```

O catálogo fica em **http://127.0.0.1:8002**, servido por comando próprio, ligado somente ao endereço local. O servidor entrega apenas `output/ui-catalog/`; não publica a raiz do repositório. Sem rota ou link na aplicação FastAPI. `python tools/frontend.py serve --port 8003` permite outra porta.

`build` gera tokens, manifesto, wrappers, sprite, CSS e catálogo. `catalog` atualiza exemplos e copia assets compilados; `watch` observa apenas a compilação do CSS. Depois de mudar templates ou JS, execute `catalog` para atualizar a cópia servida. Depois de mudar o snapshot, execute `build` completo. Catálogo gerado e imagens de QA ficam ignorados pelo Git.

## Validação e migração futura

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe tools/frontend.py check
```

`tests/test_frontend.py` confere cobertura, aliases, IDs/ARIA, renderização, escape, hashes de assets e integridade dos arquivos protegidos. `frontend/protected-files.json` registra o estado inicial dos arquivos de produto e IA desta rodada, incluindo mudanças locais que já existiam antes da biblioteca.

QA adicional opcional: `node tools/browser-check.cjs`, com Playwright e Edge instalados, catálogo iniciado na porta 8002. Node é necessário somente para esse teste de navegador, não para o build ou a aplicação. Verifica 390/768/1440 px, Light/Dark, contraste dos pares semânticos de texto e botões de ação, fontes, teclado, foco em diálogos e após swaps, menus, indeterminate e carregamento idempotente. Não equivale a auditoria completa de acessibilidade de telas futuras.

O CSS deve ser reprodutível: dois builds do mesmo snapshot e fontes têm o mesmo SHA-256. Classes Tailwind são literais nos templates ou explicitamente incluídas no scanner. Variantes usam seletores próprios em `components.css`, que não são eliminados pelo scanner; não construir utilities concatenando strings em runtime.

Antes de migrar uma tela, obter autorização específica, escolher o fluxo aprovado, verificar estados oficiais e alinhar os componentes com a referência visual. Carregar a biblioteca somente em um contêiner `.df-ui`, tratar seus eventos na integração da tela e testar responsividade/acessibilidade com dados reais autorizados ou fictícios. Remover Pico/Inter/Lucide somente quando a migração daquela tela for aprovada. Não misturar dois resets. Nenhum push, implantação ou migração de tela foi feito nesta rodada.
