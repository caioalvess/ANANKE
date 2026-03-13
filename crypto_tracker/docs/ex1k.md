# ex1k — Simulacao de Execucao Real

## O que e

O ex1k pega o order book (livro de ofertas) das duas exchanges e simula: "se eu comprasse $1.000 aqui e vendesse $1.000 ali, agora, com as ordens que existem de verdade, quanto eu lucraria?"

## Por que existe

O spread teorico diz "BTC ta $100 aqui e $101 ali, 1% de lucro". Mas na pratica, quando voce vai executar, voce nao compra tudo a $100 — voce consome varias ordens do order book, cada uma a um preco pior. Isso chama **slippage**.

Um spread de 5% no papel pode virar prejuizo se o book tiver $200 de liquidez de cada lado. O ex1k prova se o spread sobrevive a execucao real.

## Como ler

- **ex1k = 0.5%** → executando $1K em cada lado, sobram 0.5% de lucro real depois do slippage e taker fees
- **ex1k = 0.0%** → breakeven, o slippage come todo o spread
- **ex1k = -0.2%** → o spread existe no papel mas da prejuizo quando voce tenta executar (book raso)
- **ex1k = — (traco)** → sem dados de depth para esse par (fora do top-N monitorado pelo probe)

## Pra que serve na pratica

O ex1k e o filtro entre "parece bom" e "e bom de verdade". Sem ele, voce esta operando no escuro — confiando num numero teorico que ignora a realidade do order book.

### Como usar pro seu beneficio

- **ex1k positivo + Tier A**: essa oportunidade foi testada contra o book real. O lucro que aparece na tela e o que voce vai conseguir de fato executando $1K. Pode agir.
- **ex1k negativo**: nao execute. O spread existe no papel mas o book e raso demais — voce vai pagar mais caro pra comprar e receber menos pra vender do que os precos de topo sugerem. O slippage come o lucro.
- **ex1k proximo de zero (0.0-0.1%)**: breakeven. O spread cobre o slippage por pouco. Qualquer movimento de preco durante a execucao pode virar prejuizo. So vale se voce tiver execucao muito rapida (API, nao manual).
- **ex1k = traco**: sem dados. Nao significa que e ruim — o probe so monitora os top-N pares por spread. Se o par nao esta no top-N, fica sem verificacao (Tier B).

### Combinacao com outras metricas

- **ex1k positivo + AGE verde (<3s)**: cenario ideal. Execucao confirmada E dado fresco.
- **ex1k positivo + AGE vermelho (>10s)**: o ex1k foi calculado com um book que pode ter mudado. Nao confie.
- **ex1k positivo + mdq baixo (<$500)**: da lucro em $1K mas nao tem volume pra mais. Se voce quer operar $5K, esse par nao serve.
- **ex1k positivo + FREQ alta**: oportunidade recorrente e confirmada. Pode valer automatizar.

## Como funciona por dentro

1. O `OrderBookProbe` faz chamadas REST ao endpoint de order book das exchanges (top-N pares por spread)
2. Para cada lado (compra e venda), percorre o book nivel a nivel, acumulando ate $1.000 de volume
3. Calcula o preco medio ponderado real de execucao em cada lado (VWAP)
4. Aplica taker fees de cada exchange sobre o preco VWAP: `buy_cost = vwap_ask * (1 + taker_fee)`, `sell_rev = vwap_bid * (1 - taker_fee)`
5. ex1k = (sell_rev - buy_cost) / buy_cost * 100
6. O resultado ja considera tanto o impacto de mercado (slippage) quanto as taker fees das duas exchanges

## Relacao com AGE

O ex1k e calculado a partir do order book num momento especifico. Se o AGE do par esta alto (>10s), o book ja mudou e o valor do ex1k nao e mais confiavel. Por isso o sistema checa AGE antes de tudo — se age > 30s, o par vai pra Tier C independente do ex1k.

Na pratica: so confie no ex1k quando o AGE esta verde (<3s). Ex1k positivo com AGE vermelho e informacao velha.

## Relacao com o Quality Tier

O ex1k alimenta diretamente o Quality Tier (QT):

- **Tier A**: ex1k > 0 E liquidez (mdq) >= 50% do trade de referencia → execucao confirmada
- **Tier B**: sem dados de ex1k (par fora do top-N do probe) → nao verificado
- **Tier C**: ex1k <= 0 (slippage mata o spread) OU dados stale (>30s) → provavelmente inexecutavel

## Relacao com mdq (DEPTH)

O mdq e o minimo de liquidez total em USD entre os dois lados do order book. Ele complementa o ex1k:

- ex1k diz SE da lucro executando $1K
- mdq diz QUANTA liquidez existe no book pra sustentar o trade

Um ex1k positivo com mdq de $500 significa: da lucro em $1K mas o book nao tem liquidez pra preencher o trade inteiro. Por isso o tier A exige ambos.

## Onde fica no codigo

- Probe: `src/ananke/orderbook.py` — `OrderBookProbe.enrich_arb_results()`
- Enriquecimento: `src/ananke/web/server.py` — chamado no `_broadcast_tick()` apos `_compute_arbitrage()`
- Frontend: coluna "EXEC $1K" na tabela de arbitragem

## Diferencial competitivo

Nenhum scanner publico de arbitragem (ArbitrageScanner.io, Coinglass, CryptoArbitrageScreener, Bitsgap, ZipA) faz essa validacao. Todos mostram spread teorico sem verificar se o order book aguenta a execucao.
