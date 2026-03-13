# FREQ — Frequencia de Aparicao da Oportunidade

## O que e

Quantas vezes aquele par de arbitragem apareceu nos ultimos 5 minutos.

A cada ~1 segundo o ANANKE escaneia todas as exchanges e tira uma "foto" de quais oportunidades existem naquele instante. O FREQ conta: em quantas dessas fotos aquele par apareceu?

## Por que existe

Nem toda oportunidade de arbitragem e igual. Algumas existem por minutos, outras por 2 segundos. O spread bruto nao te diz isso — voce ve "1.2%" mas nao sabe se aquilo ja tava ali ha 3 minutos ou se apareceu agora e vai sumir em 1 segundo.

O FREQ separa oportunidades estruturais (spread persistente entre exchanges) de flashes (desalinhamento momentaneo que corrige sozinho). Essa distincao muda completamente como voce age.

## Como ler

O window e de 5 minutos (300 segundos), com snapshots a cada ~1s. Ou seja, o maximo teorico e ~300.

- **FREQ > 100**: spread persistente. A oportunidade aparece em mais de 1/3 do tempo. Nao e acidente, nao e glitch — e um spread estrutural entre as exchanges.
- **FREQ 20-100**: spread intermitente. Aparece e some. O preco oscila ao redor do ponto de equilibrio.
- **FREQ < 20**: flash. Apareceu poucas vezes em 5 minutos. Pode ter sido lucrativo mas voce provavelmente nao vai conseguir pegar manualmente.
- **FREQ = 0**: o par nao apareceu no window de 5 minutos. So aparece se o dado ainda esta no buffer mas saiu do window.

## Pra que serve na pratica

O FREQ te diz qual estrategia usar pra cada oportunidade.

### Spreads persistentes (FREQ alta)

FREQ > 100 significa que o spread fica ali por minutos. Voce tem tempo pra avaliar, verificar o book, abrir a exchange, executar com calma. Sao geralmente spreads menores (0.3-0.8%) mas consistentes.

Uso: ideais pra quem opera com volume ou quer automatizar. Se o spread de 0.5% aparece 200 vezes em 5 minutos, voce pode executar repetidamente. E renda previsivel, nao aposta.

### Spreads intermitentes (FREQ media)

FREQ 20-100 significa que pisca. Viavel se voce ja esta posicionado e com as exchanges abertas, mas nao da pra ir com calma.

Uso: pre-posicione capital nas duas exchanges. Quando o par aparecer com AGE verde, execute imediatamente. Nao ha tempo pra analise.

### Flashes (FREQ baixa)

FREQ < 20 — apareceu poucas vezes. Ou o spread e enorme e corrige rapido (alguem viu e executou), ou e ruido de preco.

Uso: nao tente pegar manualmente. Mas se esse par tambem tem ex1k positivo quando aparece, vale colocar em watchlist. Quando aparecer de novo, age rapido. Ou melhor: automatize.

## Combinacao com outras metricas

### FREQ + AGE

- **FREQ alta + AGE verde**: o spread esta ali agora, e fresco. Cenario de acao.
- **FREQ alta + AGE vermelho**: o spread existiu bastante no passado recente mas o dado atual esta velho. Espere o AGE voltar pro verde.
- **FREQ baixa + AGE verde**: flash raro e fresco. Se ex1k e positivo, aja rapido — pode nao voltar.

### FREQ + ex1k

- **FREQ alta + ex1k positivo**: melhor combinacao possivel. Spread recorrente E confirmado pelo book. Esse par da dinheiro de verdade.
- **FREQ alta + ex1k negativo**: armadilha. O spread aparece frequentemente mas nunca e executavel — book raso cronico. O preco teorico e bom, a execucao real da prejuizo. Ignore.
- **FREQ alta + ex1k = traco**: spread recorrente mas fora do top-N do probe. Pode valer investigacao manual — se o book for decente, e oportunidade nao explorada.

### FREQ + DUR

- **FREQ alta + DUR longo**: o spread esta ali agora E ja esta la faz tempo. Oportunidade estavel que pode ser explorada repetidamente.
- **FREQ alta + DUR curto**: o spread e frequente (aparece muito) mas cada aparicao dura pouco. Mais dificil de capturar.
- **FREQ baixa + DUR longo**: raro mas quando aparece, fica. Se ex1k confirmar, vale posicionar e esperar.

### FREQ + mdq

- **FREQ alta + mdq alto**: spread recorrente com liquidez. Escala. Voce pode operar tamanho nesse par.
- **FREQ alta + mdq baixo**: spread recorrente mas book raso. So pra trades pequenos. Nao tente forcar volume.

### O cenario ideal

**FREQ alta + AGE verde + Tier A + mdq alto**: spread persistente, dado fresco, execucao confirmada por depth probe, liquidez disponivel. Esse par e dinheiro na mesa.

## Relacao com o Quality Tier

O FREQ nao entra diretamente no calculo do Quality Tier (que depende de ex1k, mdq e AGE). Mas na pratica, pares com FREQ alta tendem a oscilar entre Tier A e B conforme o probe roda — as vezes o depth confirma, as vezes nao da tempo. O FREQ te diz que vale a pena prestar atencao mesmo quando o tier momentaneo e B.

## Onde fica no codigo

- Coleta: `src/ananke/metrics.py` — `MetricsCollector.record()` registra cada snapshot
- Calculo: `src/ananke/metrics.py` — `MetricsCollector.get_pair_freq()` conta aparicoes no window
- Enriquecimento: `src/ananke/metrics.py` — `MetricsCollector.enrich_arb_results()` adiciona `freq` ao resultado
- Frontend: coluna "FREQ" na tabela de arbitragem

## Diferencial competitivo

ArbitrageScanner tem "pair lifetime" (quanto tempo o spread existe em segundos), que e um conceito proximo mas invertido: mede a duracao atual, nao a frequencia historica. O FREQ do ANANKE e mais informativo porque mostra padrao de recorrencia — um spread que aparece 200x em 5 minutos diz mais do que "existe ha 45 segundos".

Nenhum outro scanner publico mostra frequencia de aparicao.
