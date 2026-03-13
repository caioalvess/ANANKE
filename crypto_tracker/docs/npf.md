# NET PROFIT % (npf) — Lucro Apos Taker Fees

## O que e

O lucro percentual do spread depois de descontar as taker fees das duas exchanges envolvidas. E a segunda camada de profit do ANANKE — entre o spread bruto (pf) e o lucro final (tnpf).

## Por que existe

O spread bruto (pf) e uma mentira otimista. Ele assume que voce compra e vende sem pagar nada pras exchanges. Na realidade, toda execucao a mercado (market order / taker) paga uma taxa — tipicamente 0.1% a 0.4% dependendo da exchange. E voce paga dos dois lados: na compra E na venda.

O npf revela quanto desse spread sobra depois das taker fees. E a primeira dose de realidade.

## Taker fees por exchange

O ANANKE usa as seguintes taker fees (nivel basico, sem desconto por volume):

| Exchange | Taker Fee |
|----------|-----------|
| Binance  | 0.10%     |
| Bybit    | 0.10%     |
| KuCoin   | 0.10%     |
| Gate.io  | 0.20%     |
| Kraken   | 0.26%     |

Essas taxas estao definidas em `src/ananke/fee_registry.py` — `_DEFAULT_TAKER`.

## Como e calculado

Exemplo real:

```
BTC ask na Kraken:  $60.000 (voce compra aqui)
BTC bid na Binance: $60.600 (voce vende aqui)

1. Custo real de compra (ask + taker fee da Kraken 0.26%):
   $60.000 * 1.0026 = $60.156

2. Receita real de venda (bid - taker fee da Binance 0.1%):
   $60.600 * 0.999 = $60.539,40

3. NET PROFIT %:
   (60539.40 - 60156) / 60156 * 100 = 0.638%

Comparacao:
- pf (bruto):   1.0%
- npf (net):    0.638%
- Perdido em fees: 0.362%
```

Formula:
```
buy_cost     = ask * (1 + taker_fee_ask_exchange)
sell_revenue = bid * (1 - taker_fee_bid_exchange)
npf          = (sell_revenue - buy_cost) / buy_cost * 100
```

Note que as fees sao assimetricas — se voce compra numa exchange cara (Kraken 0.26%) e vende numa barata (Binance 0.1%), o impacto total e diferente de comprar na barata e vender na cara. O npf leva isso em conta automaticamente.

## Como ler

- **npf positivo (verde/amarelo)**: o spread sobrevive as taker fees. Tem lucro depois de pagar as exchanges.
- **npf >= 0.5% (amarelo)**: spread saudavel apos fees. Margem confortavel.
- **npf proximo de zero (0.0-0.1%)**: breakeven. As taker fees consomem quase todo o spread.
- **npf negativo (vermelho)**: prejuizo. As taker fees sozinhas ja superam o spread. Nao execute.

## Pra que serve na pratica

### Primeiro filtro de viabilidade

O npf e o primeiro corte entre fantasia e possibilidade. Se o npf ja e negativo, nao precisa olhar mais nada — nao importa se o book e gordo, se o dado e fresco, se o DUR e longo. As fees ja mataram o trade.

### Diagnostico de custo: taker fees

Comparando pf com npf voce ve exatamente quanto as taker fees estao custando:

```
Impacto das fees = pf - npf
```

Se pf = 1.0% e npf = 0.5%, as taker fees estao custando 0.5 pontos percentuais.

Isso te ajuda a decidir:

- **Trocar de exchange**: se voce compra na Kraken (0.26%), talvez a mesma moeda na KuCoin (0.1%) tenha um ask proximo. Economia de 0.16% por lado.
- **Usar limit orders**: taker fees se aplicam a market orders. Limit orders pagam maker fees, que sao menores ou ate zero em algumas exchanges. O trade-off: voce perde velocidade de execucao.
- **Negociar fee tier**: com volume alto, as exchanges reduzem taker fees. Se voce opera $100K/mes na Binance, a taker cai de 0.1% pra 0.08% ou menos.

### Assimetria entre exchanges

O npf revela quando uma exchange e significativamente mais cara que a outra. Se voce ve o mesmo par com npf muito diferente dependendo de qual lado e compra e qual e venda, a causa sao as fees assimetricas.

Exemplo:
- Comprar Kraken (0.26%) → Vender Binance (0.1%): custo total de fees ~0.36%
- Comprar Binance (0.1%) → Vender Kraken (0.26%): custo total de fees ~0.36%

Nesse caso e simetrico. Mas se uma exchange tem taker de 0.05% (fee tier alto) e outra tem 0.3%, a direcao do trade importa.

### Ponte pro tnpf

O npf e o input direto do tnpf:

```
tnpf = npf - (withdrawal_fee / ref_trade_size) * 100
```

Se o npf ja e negativo, o tnpf so pode ser pior. Se o npf e positivo, o tnpf te diz se a withdrawal fee mata o lucro restante.

## Combinacao com outras metricas

### npf + pf

- **pf alto + npf alto**: spread real e gordo. As fees nao consomem muito. Cenario ideal.
- **pf alto + npf baixo/negativo**: o spread parece bom mas as fees destroem. Tipico quando as duas exchanges sao caras ou quando o spread e pequeno mas parece grande percentualmente.
- **pf baixo + npf negativo**: spread minimo que nao sobrevive a nada. Ignore.

### npf + tnpf

- **npf positivo + tnpf positivo**: lucro sobrevive tanto as taker fees quanto a withdrawal. Trade viavel.
- **npf positivo + tnpf negativo**: taker fees OK mas withdrawal fee mata. Solucoes: opere tamanho maior (dilui a wf), use rede mais barata, ou pre-posicione capital.
- **npf negativo + tnpf negativo**: morto em ambas as camadas. Ignore.

### npf + ex1k

- **npf positivo + ex1k positivo**: lucro teorico confirmado pelo book real. Forte.
- **npf positivo + ex1k negativo**: as fees estao cobertas mas o book e raso. O slippage real vai comer o lucro que o npf prometeu.
- **npf negativo + ex1k positivo**: caso raro. O book aguenta mas as fees nao deixam. Nao execute.

### npf + AGE

- **npf positivo + AGE verde**: lucro real com dado fresco. Pode agir.
- **npf positivo + AGE vermelho**: o npf foi calculado com precos velhos. O spread pode ter fechado.

### npf + FREQ

- **npf positivo + FREQ alta**: lucro recorrente apos fees. Candidato pra automacao.
- **npf negativo + FREQ alta**: spread frequente que nunca da lucro. Armadilha cronica — ignore.

## Relacao com o Quality Tier

O npf nao entra diretamente no calculo do Quality Tier (que usa ex1k e mdq). Mas na pratica:

- Tier A com npf negativo e contraditorio — se o ex1k e positivo, o npf deveria ser tambem na maioria dos casos (o ex1k ja inclui tanto slippage quanto taker fees, e o slippage so piora o preco)
- Tier B e C sao ordenados por tnpf, que depende diretamente do npf

## Onde fica no codigo

- Calculo: `src/ananke/fee_registry.py` — `FeeRegistry.net_profit_after_taker()`, formula `(sell_revenue - buy_cost) / buy_cost * 100`
- Taker fees: `src/ananke/fee_registry.py` — `_DEFAULT_TAKER` dict
- Chamada: `src/ananke/web/server.py` — `_compute_arbitrage()`, linha 146
- Frontend: coluna "NET PROFIT %" na tabela de arbitragem
- Cor: amarelo se >= 0.5%, verde se > 0%, vermelho se < 0%

## Diferencial competitivo

Muitos scanners mostram um "profit" unico que ou e bruto (ignora fees) ou ja inclui tudo junto (nao da pra saber onde o lucro se perde). O ANANKE separa pf, npf e tnpf em 3 colunas distintas.

ZipA mostra "net profit" que inclui trading + withdrawal fees num unico numero. Nao separa as camadas — voce ve o resultado final mas nao sabe se o problema sao as taker fees ou o custo de withdrawal.

O npf separado permite diagnostico preciso: se pf e bom mas npf e ruim, voce sabe que o problema sao as taker fees e pode agir especificamente sobre isso (trocar exchange, usar limit order, negociar fee tier).
