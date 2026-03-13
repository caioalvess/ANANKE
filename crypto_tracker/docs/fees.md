# Taker Fees — Taxas de Trading por Exchange

## O que sao

As taker fees sao as taxas que cada exchange cobra quando voce executa uma market order (ordem a mercado). Em arbitragem, voce compra numa exchange e vende em outra — as duas operacoes sao market orders, entao voce paga taker fee dos dois lados.

## Fees usadas no sistema

O ANANKE usa as taker fees do tier base (VIP 0) de cada exchange, sem nenhum desconto:

| Exchange | Taker Fee | Maker Fee | Tipo de conta |
|----------|-----------|-----------|---------------|
| Binance  | 0.10%     | 0.10%     | Regular (VIP 0) |
| Bybit    | 0.10%     | 0.10%     | Regular (VIP 0) |
| KuCoin   | 0.10%     | 0.10%     | Regular (LV 0) |
| Gate.io  | 0.20%     | 0.10%     | Regular (VIP 0) |
| Kraken   | 0.26%     | 0.16%     | Pro (< $50K/mes) |

Essas sao as taxas publicadas por cada exchange para spot trading no tier mais baixo, verificadas em marco de 2026.

**Nota sobre Kraken**: existe confusao online entre 0.26% e 0.40%. O 0.40% e da interface "Instant Buy" (compra rapida para consumidor). O 0.26% e do Kraken Pro (trading via API / order book), que e o que usamos. A interface Pro e a unica relevante para arbitragem.

**Nota sobre Gate.io**: algumas fontes reportam 0.1%/0.1%, mas a estrutura atual e maker 0.1% / taker 0.2%. Como usamos market orders (taker), o sistema usa 0.20%.

## Impacto no calculo

As taker fees entram no calculo do npf (NET PROFIT %):

```
buy_cost     = ask * (1 + taker_fee_exchange_compra)
sell_revenue = bid * (1 - taker_fee_exchange_venda)
npf          = (sell_revenue - buy_cost) / buy_cost * 100
```

Exemplo com Kraken (compra) → Binance (venda), spread de 1%:
```
Compra:  $60.000 * 1.0026 = $60.156  (Kraken taker 0.26%)
Venda:   $60.600 * 0.999  = $60.539  (Binance taker 0.10%)
npf = (60539 - 60156) / 60156 * 100 = 0.638%
Custo total em fees: 1.0% - 0.638% = 0.362%
```

As fees tambem entram no ex1k, que aplica taker fees sobre o preco VWAP do order book real.

## O sistema e conservador

O ANANKE calcula com o pior cenario realista: tier base, market order, sem desconto por token nativo. Isso significa que o npf e o tnpf exibidos sao o piso — o lucro real pode ser maior se voce tiver qualquer uma das vantagens abaixo.

## Como reduzir as fees na pratica

### 1. Pagar fee com token nativo da exchange

Cada exchange oferece desconto se voce paga a taxa usando o token dela:

| Exchange | Token | Desconto |
|----------|-------|----------|
| Binance  | BNB   | ate 25%  |
| KuCoin   | KCS   | ate 20%  |
| Gate.io  | GT    | ate 20%  |
| Bybit    | —     | ate 25% (via programa proprio) |
| Kraken   | KRAK  | ate 15%  |

Exemplo: Binance taker 0.10% com desconto BNB vira 0.075%. Parece pouco, mas em 100 trades isso e significativo.

### 2. Subir de VIP tier com volume

Quanto mais voce opera, menor a fee. Exemplos de reducao no tier mais alto:

| Exchange | Tier mais alto | Taker Fee |
|----------|----------------|-----------|
| Binance  | VIP 9          | 0.02%     |
| Bybit    | VIP 5+         | 0.06%     |
| KuCoin   | LV 12          | 0.05%     |
| Gate.io  | VIP 14         | 0.03%     |
| Kraken   | $10M+/mes      | 0.10%     |

Na pratica, so os primeiros tiers importam — a maior queda de fee acontece entre VIP 0 e VIP 1/2.

### 3. Usar limit orders (maker fee)

Market orders pagam taker fee. Limit orders pagam maker fee, que e menor:

| Exchange | Taker → Maker | Economia |
|----------|---------------|----------|
| Kraken   | 0.26% → 0.16% | 0.10%   |
| Gate.io  | 0.20% → 0.10% | 0.10%   |
| Binance  | 0.10% → 0.10% | 0.00%   |
| Bybit    | 0.10% → 0.10% | 0.00%   |
| KuCoin   | 0.10% → 0.10% | 0.00%   |

Nas exchanges onde maker = taker (Binance, Bybit, KuCoin no tier base), nao ha vantagem. Mas na Kraken e Gate.io, usar limit order economiza 0.10% por lado.

**Trade-off**: limit orders nao tem garantia de execucao. Em arbitragem, velocidade importa — o spread pode fechar enquanto sua ordem espera. Limit orders funcionam melhor em spreads persistentes (FREQ alta + DUR longo) onde voce tem tempo.

### 4. Combinar descontos

Os descontos geralmente se acumulam:
- VIP 1 na Binance: taker cai de 0.10% pra 0.09%
- Com BNB: cai mais 25%, de 0.09% pra ~0.0675%
- Resultado: quase metade da fee base

## O que isso significa pro ANANKE

O sistema mostra npf e tnpf calculados com o pior cenario. Se voce opera com descontos, o lucro real e maior do que o exibido. Isso e intencional — preferimos subestimar o lucro e voce se surpreender positivamente do que o contrario.

Se voce quer calcular o npf real com suas fees pessoais, use a formula:
```
buy_cost     = ask * (1 + sua_taker_fee_compra)
sell_revenue = bid * (1 - sua_taker_fee_venda)
npf_real     = (sell_revenue - buy_cost) / buy_cost * 100
```

## Onde fica no codigo

- Definicao: `src/ananke/fee_registry.py` — `_DEFAULT_TAKER` dict
- Lookup: `src/ananke/fee_registry.py` — `FeeRegistry.taker_fee()` retorna a fee por exchange
- Uso no npf: `src/ananke/fee_registry.py` — `FeeRegistry.net_profit_after_taker()`
- Uso no ex1k: `src/ananke/orderbook.py` — `_apply_depth()`, aplica taker sobre VWAP
- Fallback: se uma exchange nao esta no dict, o sistema usa 0.10% como default

## Fontes de verificacao (marco 2026)

- Binance: [BitDegree — Binance Fees](https://www.bitdegree.org/crypto/tutorials/binance-fees)
- Bybit: [BitDegree — Bybit Fees](https://www.bitdegree.org/crypto/tutorials/bybit-fees)
- KuCoin: [BitDegree — KuCoin Fees](https://www.bitdegree.org/crypto/tutorials/kucoin-fees)
- Kraken: [Bitget — Kraken Pro Fees 2026](https://www.bitget.com/academy/kraken-pro-fees-2026)
- Gate.io: [Coin Bureau — Gate.com Review](https://coinbureau.com/review/gate-com-review)
