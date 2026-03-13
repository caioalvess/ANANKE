# TRUE NET % (tnpf) — Lucro Real Depois de Tudo

## O que e

O numero final. E o que sobra no seu bolso depois de descontar taker fees das duas exchanges e o custo de withdrawal pra rebalancear a posicao. Se esse numero e negativo, voce esta pagando pra operar.

## Por que existe

O spread bruto (PROFIT %) e uma ilusao. Ele ignora todos os custos de execucao. Um spread de 1% pode virar prejuizo de 2% depois de fees.

O ANANKE mostra 3 camadas de profit justamente pra voce ver onde o lucro se perde:

1. **PROFIT % (pf)**: spread bruto — diferenca de preco pura entre exchanges
2. **NET PROFIT % (npf)**: apos descontar taker fees das duas exchanges
3. **TRUE NET % (tnpf)**: apos descontar taker fees + custo de withdrawal

Cada camada revela um tipo de custo. As 3 separadas te dizem nao so SE da lucro, mas ONDE o lucro se perde quando nao da.

## Como e calculado

Exemplo real:

```
BTC ask na Kraken:  $60.000
BTC bid na Binance: $60.600

1. PROFIT % (pf)
   (60600 - 60000) / 60000 * 100 = 1.0%

2. NET PROFIT % (npf)
   Compra na Kraken: $60.000 * 1.004 (taker 0.4%) = $60.240 (custo real)
   Venda na Binance: $60.600 * 0.999 (taker 0.1%) = $60.539 (receita real)
   npf = (60539 - 60240) / 60240 * 100 = 0.496%

3. TRUE NET % (tnpf)
   Withdrawal fee BTC na Kraken: 0.0001 BTC * $60.600 = $6.06
   Trade de referencia: $1.000
   Impacto: ($6.06 / $1.000) * 100 = 0.606%
   tnpf = 0.496% - 0.606% = -0.11%

Resultado: spread de 1% virou prejuizo de 0.11% depois de tudo.
```

Formula:
```
tnpf = npf - (withdrawal_fee_em_usd / ref_trade_size) * 100
```

O ref_trade_size padrao e $1.000. Isso significa que o tnpf mostra o lucro real pra um trade de $1K.

## Como ler

- **tnpf positivo (verde/amarelo)**: da lucro real depois de todos os custos. Quanto maior, melhor.
- **tnpf = 0**: breakeven. Cobre exatamente os custos. Nao vale a pena pelo risco.
- **tnpf negativo (vermelho)**: prejuizo. Os custos superam o spread. Nao execute.
- **tnpf >= 0.5% (amarelo)**: spread saudavel. Margem suficiente pra absorver variacoes de preco durante execucao.

## Pra que serve na pratica

### Filtro de realidade

A maioria dos spreads que parecem bons no PROFIT % viram negativos no TRUE NET %. O tnpf e o filtro que separa oportunidade real de ilusao. Se voce so olha pro pf, vai executar trades que dao prejuizo.

### Diagnostico de custo

As 3 camadas separadas te dizem exatamente onde o lucro se perde:

- **pf bom, npf ruim**: o problema sao as taker fees. As exchanges cobram muito. Solucao: procure exchanges com fees menores, use maker orders (limit) em vez de taker (market), ou negocie fee tier com volume.
- **npf bom, tnpf ruim**: o problema e o custo de withdrawal. Solucoes:
  - **Opere tamanho maior**: a withdrawal fee e fixa (ex: 0.0001 BTC = $6). Em $1K de trade, isso e 0.6%. Em $10K, e 0.06%. O tnpf melhora com tamanho.
  - **Use rede mais barata**: se a exchange permite withdrawal por TRC20 em vez de ERC20, o custo cai drasticamente. (O ANANKE ainda nao exibe qual rede — gap identificado na analise competitiva.)
  - **Pre-posicione capital**: mantenha saldo nas duas exchanges e opere sem transferir. Elimina o custo de withdrawal completamente (modelo hedge).

### Dimensionamento pelo tnpf

O tnpf e calculado com ref_trade_size de $1.000. Se voce opera $5.000, o impacto real da withdrawal fee e 5x menor. Use a formula pra recalcular mentalmente:

```
tnpf_real = npf - (withdrawal_fee / seu_trade_size) * 100
```

Um par com npf = 0.5% e wf = $30:
- Trade de $1K: tnpf = 0.5% - 3.0% = -2.5% (prejuizo)
- Trade de $5K: tnpf = 0.5% - 0.6% = -0.1% (breakeven)
- Trade de $10K: tnpf = 0.5% - 0.3% = +0.2% (lucro)

## Combinacao com outras metricas

### tnpf + ex1k

- **tnpf positivo + ex1k positivo**: o spread da lucro na teoria (tnpf) E na pratica (ex1k confirma que o book aguenta). Cenario de execucao.
- **tnpf positivo + ex1k negativo**: os custos estao cobertos mas o book e raso. O slippage real pode comer o lucro que o tnpf prometeu.
- **tnpf negativo + ex1k positivo**: caso raro. O book aguenta mas os custos sao altos demais pra $1K. Pode ser viavel com trade size maior (recalcule o tnpf).

### tnpf + AGE

- **tnpf positivo + AGE verde**: lucro real com dado fresco. Pode agir.
- **tnpf positivo + AGE vermelho**: o tnpf foi calculado com precos velhos. O spread pode ter fechado. Espere dado fresco.

### tnpf + FREQ

- **tnpf positivo + FREQ alta**: lucro real e recorrente. Candidato forte pra automacao.
- **tnpf negativo + FREQ alta**: spread frequente mas que nunca da lucro depois de custos. Ignore completamente — e armadilha recorrente.

### tnpf + DUR

- **tnpf positivo + DUR longo**: lucro real aberto ha tempo. Se ninguem executou, investigue (withdrawal suspenso?). Se nao ha barreira, e dinheiro na mesa.
- **tnpf negativo + DUR longo**: explica por que ninguem executa — da prejuizo.

### tnpf + mdq

- **tnpf positivo + mdq alto**: lucro real com liquidez pra escalar. Melhor combinacao pra trades grandes.
- **tnpf positivo + mdq baixo**: da lucro mas so em tamanho pequeno.

### O cenario ideal

**tnpf >= 0.5% + Tier A + FREQ alta + DUR longo + mdq alto**: lucro real apos todos os custos, confirmado por depth probe, recorrente, sustentado, com liquidez. E o sinal mais completo que o sistema pode dar.

## Relacao com o Quality Tier

O tnpf e usado como criterio de ordenacao dentro dos tiers:

- **Tier 1 (A)**: ordenado por ex1k (execucao real tem prioridade)
- **Tier 2 (B) e Tier 3 (C)**: ordenados por tnpf (na ausencia de dados de depth, o lucro teorico apos custos e o melhor proxy)

## Relacao com a coluna TAXA ENVIO (wf)

A TAXA ENVIO (wf) e o valor absoluto da withdrawal fee em USD. O tnpf usa esse valor pra calcular o impacto percentual:

```
impacto = (wf / ref_trade_size) * 100
tnpf = npf - impacto
```

Se wf = $30 e ref_trade_size = $1.000, o impacto e 3%. Se o npf era 0.5%, o tnpf fica -2.5%.

A wf aparece como coluna separada pra voce ver o custo absoluto. O tnpf mostra o impacto percentual. Os dois juntos te permitem decidir se vale aumentar o tamanho do trade pra diluir o custo.

## Onde fica no codigo

- Calculo: `src/ananke/web/server.py` — `_compute_arbitrage()`, formula `tnpf = net_pf - (wf / ref_size) * 100`
- ref_trade_size: `src/ananke/config.py` — `ArbitrageConfig.ref_trade_size` (default $1.000)
- Frontend: coluna "TRUE NET %" na tabela de arbitragem
- Cor: amarelo se >= 0.5%, verde se > 0%, vermelho se < 0%

## Diferencial competitivo

A maioria dos scanners mostra um unico numero de "profit" — ou e bruto (sem fees) ou e parcialmente liquido (so taker fees). Ninguem mostra as 3 camadas separadas.

ZipA e o que chega mais perto: mostra "net profit" com trading + withdrawal fees. Mas nao separa as camadas — voce ve o numero final mas nao sabe se o problema e taker fee ou withdrawal fee.

Bitsgap mostrava "estimated profit" com fees descontadas, mas a feature foi descontinuada.

O ANANKE e o unico que mostra pf, npf e tnpf separados, permitindo diagnostico preciso de onde o lucro se perde.
