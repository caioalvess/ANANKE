# Analise Competitiva: ANANKE vs Plataformas de Arbitragem

**Data**: 2026-03-13
**Benchmark principal**: ArbitrageScanner.io (concorrente mais direto — spot CEX arbitrage scanner)

---

## Metricas que o ANANKE exibe (17 colunas na arb table)

| # | Coluna | Campo | Descricao |
|---|--------|-------|-----------|
| 1 | # | idx | Posicao na lista |
| 2 | QT | qt | Quality Tier (A/B/C) — baseado em depth probe |
| 3 | PAR | s, b, q | Par base/quote |
| 4 | PROFIT % | pf | Spread bruto: (bid - ask) / ask * 100 |
| 5 | NET PROFIT % | npf | Apos taker fees em ambos lados |
| 6 | TRUE NET % | tnpf | Apos taker + withdrawal fee (ref $1K trade) |
| 7 | TAXA ENVIO | wf | Custo de withdrawal do ask exchange em USD |
| 8 | COMPRA (ASK) | ak | Melhor preco de compra |
| 9 | EXCHANGE COMPRA | ax | Exchange com melhor ask (link direto) |
| 10 | VENDA (BID) | bi | Melhor preco de venda |
| 11 | EXCHANGE VENDA | bx | Exchange com melhor bid (link direto) |
| 12 | MIN VOL | msv | min(bid_vol, ask_vol) — gargalo de liquidez |
| 13 | EXEC $1K | ex1k | Slippage real via order book num trade de $1K |
| 14 | DEPTH | mdq | Liquidez no midpoint do spread |
| 15 | AGE | age | Tempo desde ultimo update (3 faixas de cor + badge STALE) |
| 16 | FREQ | freq | Quantas vezes o par apareceu nos ultimos 5 min |
| 17 | DUR | dur | Ha quanto tempo o par esta continuamente ativo |

### Metrics View (cards globais)

- **Active Now** — pares com oportunidade ativa agora
- **Seen (5m)** — pares unicos vistos em 5 min
- **Avg Spread** — spread medio no window
- **Buffer** — segundos de dados acumulados
- **Top Exchanges** — ranking de exchanges por frequencia

---

## O que o ANANKE tem que nenhum concorrente tem

| Metrica | Por que importa |
|---------|-----------------|
| **EXEC $1K (ex1k)** | Prova via order book real se o spread sobrevive a execucao. Nenhum scanner publico faz isso. |
| **DEPTH (mdq)** | Mostra liquidez real no midpoint, nao volume teorico de 24h. |
| **Quality Tier (A/B/C)** | Classifica confiabilidade: A = confirmado por depth, B = sem dados, C = non-executable ou stale. |
| **3 camadas de profit** | pf → npf → tnpf. Concorrentes mostram 1 numero. ANANKE mostra onde exatamente o lucro se perde. |
| **AGE com 3 faixas + STALE** | Fresh (<3s verde), Warn (3-10s amarelo), Old (>10s vermelho), STALE badge (>30s com opacity). |

---

## O que os concorrentes mostram que o ANANKE NAO mostra

### CRITICO

| Metrica | Quem tem | Impacto |
|---------|----------|---------|
| **Withdraw/Deposit status** (aberto/fechado por rede) | ArbitrageScanner, CryptoArbitrageScreener | User pode tentar executar arb onde withdraw esta suspenso. O ANANKE ja tem `withdraw_blocked`/`deposit_blocked` no FeeRegistry mas nao exibe no frontend. |
| **Rede de transferencia** (ERC20, TRC20, SOL, etc.) | ArbitrageScanner, ZipA | User ve "taxa = $30" mas nao sabe se e ERC20 (lento/caro) ou TRC20 (rapido/barato). Afeta tempo e custo real. |

### IMPORTANTE

| Metrica | Quem tem | Impacto |
|---------|----------|---------|
| **Gas fee / custo de rede** | ZipA | Custo de gas nao esta no calculo de tnpf. |
| **Tempo estimado de transferencia** | Nenhum (gap do mercado inteiro) | Ninguem mostra, seria diferencial. |

### NICE-TO-HAVE

| Metrica | Quem tem | Impacto |
|---------|----------|---------|
| **Max Profit % filter** (teto) | ArbitrageScanner | Spreads >100% sao quase sempre non-executable. |
| **Buy/Sell exchange filter independente** | ArbitrageScanner | ANANKE filtra por envolvimento, nao por lado. |
| **Volume em tokens + USD** (dual) | ArbitrageScanner | ANANKE mostra so USD. |

---

## Comparativo direto: ANANKE vs ArbitrageScanner.io

### ArbitrageScanner — colunas do spot screener

- Buying Exchange
- Selling Exchange
- Buy Price / Sell Price
- Volume (tokens + USD)
- Profit %
- Withdrawal Network (chain)
- Network Status (verde/vermelho)
- Pair Lifetime (segundos — conceito similar ao FREQ/DUR do ANANKE)

### ArbitrageScanner — filtros

- Exchanges buy/sell independentes
- Whitelist/blacklist de moedas
- Min/Max profit %
- Min transaction amount ($)
- Min/Max lifetime (segundos)
- Filtro por rede de withdrawal/deposit
- Filtro por status de rede (aberto/fechado)

### O que ArbitrageScanner NAO tem que ANANKE tem

- Depth probe (ex1k, mdq)
- Quality tier system
- Decomposicao taker vs withdrawal
- NET PROFIT % e TRUE NET % separados
- AGE granular com 3 faixas
- Metrics view com historico de 5 min

---

## Outros concorrentes avaliados

### Coinglass
- Foco em funding rate arbitrage (spot vs perpetual), nao spot-spot
- Mostra: funding rate, PNL, APR, revenue projetada
- Portfolio-size-aware (slider ajusta calculo de revenue)
- Nao concorre diretamente com ANANKE no spot

### CryptoArbitrageScreener.com
- Simples: coin, buy/sell exchange, buy/sell price, profit %, deposit/withdrawal status
- Atualiza a cada 15 minutos (vs ~1s do ANANKE)
- 73 exchanges, 1500+ coins

### Bitsgap
- Feature de arbitrage parcialmente descontinuada
- Mostrava: spread %, estimated profit (com fees), tipo de arb
- Fees deduzidas do profit estimado

### ZipA (mobile)
- Net profit com todas as fees (trading + withdrawal + gas)
- Info de rede por chain (ERC20, TRC20, SOL)
- 50+ exchanges
- Mobile-first

### Cryptohopper
- Bot-oriented, menos scanner
- Dashboard de execucao, nao de oportunidades
- Market Arbitrage = triangular intra-exchange

### Loris Tools
- Funding rate screener (nao spot)
- 11 CEX + 14 DEX
- Normalizacao de intervalos de funding entre exchanges

---

## Score comparativo

| Criterio | ANANKE | ArbitrageScanner | Nota |
|----------|--------|-------------------|------|
| Verossimilhanca do spread | 9/10 | 7/10 | ANANKE valida via depth probe |
| Qualidade de dados de execucao | 10/10 | 3/10 | ex1k e exclusivo |
| Decomposicao de custos | 9/10 | 6/10 | 3 camadas vs 1 |
| Status de transferencia | 2/10 | 9/10 | **Gap mais critico** |
| Freshness indicators | 9/10 | 6/10 | AGE system superior |
| Coverage de exchanges | 4/10 | 10/10 | 4 vs 75+ |
| Info de rede/chain | 0/10 | 8/10 | ANANKE nao mostra |
| UX/Visual | 8/10 | 8/10 | Par |

---

## Conclusao

Os dados que o ANANKE exibe sao **mais confiaveis e analiticamente superiores** ao que qualquer concorrente publico oferece. O depth probe + quality tier e um diferencial que ninguem tem.

A lacuna critica e **informacao operacional**: o user sabe QUE a oportunidade e real (via ex1k), mas nao sabe SE consegue executar (withdraw/deposit status) nem POR ONDE executar (chain/network).

### Prioridades de evolucao

1. **Exibir withdraw/deposit status no frontend** (dados ja existem no FeeRegistry)
2. **Exibir rede de transferencia** (requer expansao da API de fees)
3. **Incluir gas fee no calculo de tnpf**
4. **Max profit filter** (client-side, trivial)
