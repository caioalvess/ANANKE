# mdq (DEPTH) — Liquidez Real no Order Book

## O que e

O mdq (minimum depth quantity) e o minimo de liquidez total, em USD, entre os dois lados do order book (buy side e sell side). Pega a profundidade total de cada lado em todos os niveis e usa o menor — porque e o lado mais fraco que limita a execucao.

## Por que existe

Um spread de 0.8% entre Binance e Bybit parece bom. Mas se o book tiver 3 ordens de $50 cada entre o ask e o bid, voce so consegue executar $150 antes de o preco andar contra voce.

O mdq responde: "tem quantos dolares de liquidez disponivel pra eu executar antes de o preco fugir?"

## Como ler

- **mdq = $15.000** → $15K de ordens no book pra absorver seu trade, confortavel pra um trade de $1K
- **mdq = $800** → book raso, um trade de $1K ja come toda a liquidez disponivel (aparece em amarelo como alerta)
- **mdq = — (traco)** → sem dados de depth para esse par (fora do top-N monitorado pelo probe)

No frontend, valores abaixo de $1.000 aparecem em amarelo como alerta visual de liquidez insuficiente.

## Pra que serve na pratica

O mdq te diz quanto tamanho voce pode colocar. O ex1k prova que da lucro em $1K, mas se voce quer operar $5K ou $10K, precisa saber se o book aguenta. O mdq responde isso.

### Como usar pro seu beneficio

- **mdq muito acima do seu trade size** (ex: mdq = $20K e voce opera $2K): confortavel. O book absorve seu trade sem impacto relevante.
- **mdq proximo do seu trade size** (ex: mdq = $1.500 e voce opera $1K): vai funcionar mas esta no limite. Qualquer ordem grande de outro trader entre voce ver e executar pode comer a liquidez.
- **mdq abaixo do seu trade size** (ex: mdq = $400 e voce opera $1K): nao cabe. Voce vai ter que parcial fill ou aceitar slippage maior do que o ex1k indica. Reduza o tamanho do trade ou ignore esse par.
- **mdq = traco**: sem dados. Mesmo caso do ex1k — par fora do top-N do probe.

### Dimensionamento de trade

Use o mdq como teto pratico do tamanho do trade. Regra simples: opere no maximo 50-70% do mdq. Se mdq = $3.000, opere ate $2.000. Isso deixa margem pra outros participantes e pra mudancas no book entre ver e executar.

### Combinacao com outras metricas

- **mdq alto + ex1k positivo + AGE verde**: melhor cenario. Liquidez, lucro confirmado, dado fresco.
- **mdq alto + ex1k negativo**: book gordo mas precos ruins. Tem bastante volume mas espalhado em niveis que nao dao lucro. Nao adianta ter liquidez se o spread nao sobrevive.
- **mdq baixo + ex1k positivo**: da lucro mas pouco volume. Bom pra trades pequenos, nao escala.
- **mdq baixo + FREQ alta**: par que aparece frequentemente com book raso. Provavelmente e um token de baixa liquidez com spread cronico — as oportunidades existem mas sao dificeis de executar com tamanho.

## Relacao com AGE

Assim como o ex1k, o mdq e um snapshot do order book num momento especifico. Se o AGE esta alto, a liquidez pode ter mudado. Books de pares pouco liquidos mudam rapido — uma unica ordem de $500 entrando ou saindo altera o mdq drasticamente.

## Como funciona por dentro

1. O `OrderBookProbe` busca o order book via REST para os top-N pares com maior spread
2. Para cada lado (buy e sell), soma o volume total em USD de todos os niveis do book (`depth_available_quote`)
3. mdq = `min(buy_side, sell_side)` — usa o lado mais fraco como medida de profundidade real

## Relacao com ex1k

Os dois saem da mesma fonte (o order book) mas medem coisas diferentes:

- **mdq** = `min(buy_side.depth_available_quote, sell_side.depth_available_quote)` — minimo entre a profundidade total dos dois lados do book (todos os niveis). Responde: "quanto volume o lado mais fraco do book aguenta?"
- **ex1k** = simulacao VWAP percorrendo nivel a nivel ate $1K, calculando lucro real com taker fees. Responde: "da lucro executando $1K?"

### Por que um nao substitui o outro

- mdq pode ser $50.000 (book gordo) mas ex1k negativo — a liquidez esta espalhada em precos ruins, longe do topo
- mdq pode ser $1.200 (book fino) mas ex1k positivo — a pouca liquidez que tem esta concentrada perto do topo do book

Um spread de 2% com ex1k positivo mas mdq de $300 significa: da lucro no papel, mas voce nao consegue colocar tamanho relevante. O book nao aguenta.

O Tier A exige os dois justamente por isso. Um sem o outro nao garante nada.

## Relacao com o Quality Tier

O mdq alimenta o calculo de fill ratio que define o Tier A:

- fill = mdq / ref_trade_size (default $1.000)
- Se fill >= 50% E ex1k > 0 → **Tier A** (execucao confirmada)
- Se fill < 50% ou sem dados → **Tier B** (nao verificado)

Ou seja: nao basta o ex1k ser positivo. O book precisa ter liquidez suficiente pra preencher pelo menos metade do trade de referencia.

## Onde fica no codigo

- Calculo: `src/ananke/orderbook.py` — `OrderBookProbe.enrich_arb_results()`
- Tier assignment: `src/ananke/web/server.py` — `_rank_arbitrage()`, linha `fill = min(1.0, (mdq or 0) / ref_trade_size)`
- Frontend: coluna "DEPTH" na tabela de arbitragem

## Diferencial competitivo

Nenhum scanner publico mostra liquidez real do order book. Todos mostram volume de 24h, que e um numero agregado que nao diz nada sobre a liquidez disponivel agora pra executar o trade.
