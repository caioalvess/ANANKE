# Analise Competitiva v2: ANANKE vs ArbitrageScanner.io

**Data**: 2026-03-18
**Versao anterior**: 2026-03-13 (competitive_analysis.md)
**Metodologia**: Comparacao imparcial baseada em estado atual do codigo ANANKE + documentacao publica do ArbitrageScanner.io

---

## 1. Visao Geral das Plataformas

### ANANKE
- Scanner open-source / self-hosted
- 6 exchanges CEX (Binance, Bybit, OKX, Kraken, KuCoin, Gate.io)
- Spot cross-exchange + triangular intra-exchange
- Custo: $0 (self-hosted)
- Sem execucao automatica

### ArbitrageScanner.io
- SaaS comercial (Dubai — ArbitrageScan Developers LTD)
- 80+ exchanges CEX + 25+ DEX em 40+ blockchains
- Spot, futures, funding rate, cross-chain (CEX+DEX)
- Custo: $99-$795/mes (planos START a ENTERPRISE), VIP $2999/ano
- Sem execucao automatica

---

## 2. Comparativo por Criterio

### 2.1 Coverage de Exchanges

| | ANANKE | ArbitrageScanner |
|---|--------|-------------------|
| CEX | 6 | 80+ |
| DEX | 0 | 25+ (40+ blockchains) |
| Score | **3/10** | **10/10** |

**Nota**: ANANKE cobre as 6 maiores (Binance, Bybit, OKX, Kraken, KuCoin, Gate.io) que representam ~85% do volume spot global. ArbitrageScanner tem cobertura massiva incluindo exchanges de menor liquidez onde spreads maiores ocorrem. A falta de DEX no ANANKE elimina toda uma classe de oportunidades.

---

### 2.2 Tipos de Arbitragem

| Tipo | ANANKE | ArbitrageScanner |
|------|--------|-------------------|
| Cross-exchange spot (CEX-CEX) | Sim | Sim |
| Triangular intra-exchange | Sim (Bellman-Ford) | Nao |
| Spot vs Futures | Nao | Sim |
| Futures vs Futures | Nao | Sim |
| Funding Rate | Nao | Sim |
| Cross-chain (CEX-DEX) | Nao | Sim |
| DEX-DEX | Nao | Sim |
| Score | **4/10** | **9/10** |

**Nota**: ANANKE tem triangular (exclusivo) mas ArbitrageScanner cobre muito mais tipos. Funding rate e spot-futures sao estrategias de baixo risco com alta demanda.

---

### 2.3 Qualidade de Dados de Spread / Execucao

| Metrica | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Profit bruto (spread) | Sim (pf) | Sim |
| Net profit apos taker fees | Sim (npf) — taker fees assimetricos por exchange | Nao explicitamente separado |
| True net profit (com withdrawal) | Sim (tnpf) — 3 camadas decompostas | Nao — mostra profit % sem decomposicao |
| Execucao simulada (order book) | **Sim (ex1k)** — VWAP em $1K real no L2 book | **Nao** |
| Depth de liquidez real | **Sim (mdq)** — profundidade em quote no midpoint | **Nao** — mostra volume teorico |
| Quality Tier (A/B/C) | **Sim** — classificacao baseada em depth probe | **Nao** |
| Volume em tokens + USD | Nao (so USD) | Sim (dual) |
| Score | **10/10** | **5/10** |

**Nota**: Este e o maior diferencial do ANANKE. O ex1k prova se um spread sobrevive a execucao real contra o order book. Nenhum scanner publico faz isso. ArbitrageScanner mostra spread teorico sem validar contra slippage — o user ve "75% profit" mas nao sabe se ha liquidez para executar.

---

### 2.4 Tratamento de Fees

| Fee | ANANKE | ArbitrageScanner |
|-----|--------|-------------------|
| Taker fees por exchange | Sim (0.10%-0.26%, assimetrico) | Mencionado nos calculos, sem decomposicao visivel |
| Withdrawal fees por asset | Sim (multi-source: API + scrape) | Mencionado como "network fee" |
| Decomposicao visivel (3 camadas) | **Sim: pf → npf → tnpf** | **Nao — profit % unico** |
| Rede mais barata automatica | Sim (cheapest network per asset) | Nao claro |
| Score | **9/10** | **5/10** |

---

### 2.5 Informacao Operacional (Transfer)

| Info | ANANKE | ArbitrageScanner |
|------|--------|-------------------|
| Withdraw/Deposit status | **Sim (tf column)** — checkmark/X/dash no frontend | **Sim** — verde/vermelho por rede |
| Rede de transferencia (chain) | **Nao** | **Sim** — mostra ERC20, TRC20, etc |
| Status por rede individual | **Nao** — status agregado (qualquer rede) | **Sim** — status por rede |
| Filtro por rede | **Nao** | **Sim** |
| Filtro por compatibilidade de rede | **Nao** | **Sim** — "ao menos 1 rede em comum" |
| Score | **4/10** | **9/10** |

**Nota**: Desde o comparativo anterior (13/Mar), ANANKE adicionou a coluna TF no frontend (commit a4ffbca). Isso resolve o gap mais critico parcialmente — o user agora ve se a transferencia e possivel, mas nao sabe POR QUAL REDE. ArbitrageScanner mostra a rede especifica e seu status.

---

### 2.6 Freshness / Latencia de Dados

| Metrica | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Update frequency | ~1s (WebSocket primary) | ~1s (claimed) |
| Freshness indicator por opp | **Sim (AGE)** — 3 faixas: <3s verde, 3-10s amarelo, >10s vermelho, >30s STALE | **Nao visivel** |
| Fallback se WS cai | Sim — REST polling automatico | Nao documentado |
| Score | **9/10** | **6/10** |

**Nota**: ANANKE tem transparencia superior sobre freshness dos dados. O badge STALE e a degradacao visual permitem ao user avaliar confiabilidade em tempo real.

---

### 2.7 Filtros e Customizacao

| Filtro | ANANKE | ArbitrageScanner |
|--------|--------|-------------------|
| Exchange (geral) | Sim | Sim |
| Exchange buy/sell separado | **Nao** | **Sim** |
| Quote asset (USDT, BTC, etc) | Sim | Via whitelist masks (*/USDT) |
| Whitelist/blacklist moedas | **Nao** | **Sim** |
| Min profit % | Sim (config env) | Sim (0.5%+) |
| Max profit % (teto) | **Nao** | **Sim** (ate 150%) |
| Min volume/transaction | Sim (config env) | Sim |
| Min/Max lifetime | **Nao** | **Sim** (em segundos) |
| Filtro por rede | **Nao** | **Sim** |
| Filtro por status de rede | **Nao** | **Sim** |
| Search por par | Sim (frontend) | Sim |
| Score | **4/10** | **9/10** |

**Nota**: ArbitrageScanner e significativamente superior em filtros. ANANKE tem filtros basicos via env var (nao alteraveis em runtime no frontend). Os filtros de compra/venda separados e whitelist/blacklist sao funcionalidades relevantes que o ANANKE nao tem.

---

### 2.8 Analytics e Historico

| Feature | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Metrics dashboard | **Sim** — active now, seen 5m, avg spread, buffer | **Nao** |
| Top exchanges ranking | **Sim** — por frequencia | **Nao** |
| Frequencia por par (FREQ) | **Sim** — vezes em 5 min | **Nao** |
| Duracao ativa (DUR) | **Sim** — segundos continuamente ativo | Sim — "pair lifetime" |
| Time series chart | **Sim** — count over 5 min | **Nao** |
| Spread distribution | **Sim** — histograma por faixa | **Nao** |
| Historico persistente | Nao (60 min in-memory) | Nao documentado |
| Score | **8/10** | **3/10** |

**Nota**: ANANKE tem analytics muito superiores. A metrics view com time series, distribuicao de spread, e top exchanges e uma camada de inteligencia que ArbitrageScanner nao oferece.

---

### 2.9 Alertas

| Feature | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Telegram alerts | Sim | Sim |
| Min profit threshold | Sim | Sim |
| Min volume threshold | Sim | Nao claro |
| Cooldown anti-spam | Sim (configuravel) | Nao documentado |
| Modo transfer vs hedge | Sim (usa tnpf vs npf) | Nao |
| Links diretos para exchanges | Sim | Nao claro |
| Score | **8/10** | **7/10** |

---

### 2.10 UX / Interface

| Aspecto | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Estetica | Retro terminal CRT verde — coerente e funcional | SaaS moderno / polished |
| Views multiplas | Sim (Arbitrage + Triangular + Metrics) | Spot screener + Futures screener (separados) |
| Responsividade | Basica | Full responsive |
| Mobile | Nao otimizado | App-like (mobile-friendly) |
| Onboarding | Manual (env vars, self-hosted) | Wizard setup, tutoriais, guides |
| Score | **6/10** | **8/10** |

---

### 2.11 Ecossistema e Features Adicionais

| Feature | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Wallet analysis | Nao | Sim (AI-powered, 272 criterios) |
| NFT scanner | Nao | Sim |
| Keyword monitoring (Telegram/Reddit) | Nao | Sim (4s interval) |
| White-label | Nao | Sim |
| Comunidade / mentoria | Nao | Sim (VIP manager, Zoom, eventos) |
| API publica | WebSocket + REST basico | Nao documentado |
| Score | **2/10** | **9/10** |

---

### 2.12 Custo e Acessibilidade

| Aspecto | ANANKE | ArbitrageScanner |
|---------|--------|-------------------|
| Preco | $0 (self-hosted) | $99-$795/mes |
| Requer servidor | Sim | Nao (SaaS) |
| Requer conhecimento tecnico | Sim (Python, env vars) | Nao |
| Open-source | Sim | Nao |
| Score | **9/10** | **5/10** |

**Nota**: Para quem tem capacidade tecnica, ANANKE e infinitamente mais barato. ArbitrageScanner cobra ate $9,590/ano no plano mais alto.

---

## 3. Score Final Comparativo

| Criterio | Peso | ANANKE | ArbitrageScanner |
|----------|------|--------|-------------------|
| Coverage de exchanges | 15% | 3 | 10 |
| Tipos de arbitragem | 10% | 4 | 9 |
| Qualidade dados execucao | 20% | **10** | 5 |
| Tratamento de fees | 10% | **9** | 5 |
| Info operacional (transfer) | 10% | 4 | **9** |
| Freshness / latencia | 5% | **9** | 6 |
| Filtros e customizacao | 5% | 4 | **9** |
| Analytics e historico | 5% | **8** | 3 |
| Alertas | 5% | **8** | 7 |
| UX / Interface | 5% | 6 | 8 |
| Ecossistema adicional | 5% | 2 | 9 |
| Custo / acessibilidade | 5% | **9** | 5 |

### Score ponderado

**ANANKE**: (3×0.15)+(4×0.10)+(10×0.20)+(9×0.10)+(4×0.10)+(9×0.05)+(4×0.05)+(8×0.05)+(8×0.05)+(6×0.05)+(2×0.05)+(9×0.05) = 0.45+0.40+2.00+0.90+0.40+0.45+0.20+0.40+0.40+0.30+0.10+0.45 = **6.45/10**

**ArbitrageScanner**: (10×0.15)+(9×0.10)+(5×0.20)+(5×0.10)+(9×0.10)+(6×0.05)+(9×0.05)+(3×0.05)+(7×0.05)+(8×0.05)+(9×0.05)+(5×0.05) = 1.50+0.90+1.00+0.50+0.90+0.30+0.45+0.15+0.35+0.40+0.45+0.25 = **7.15/10**

---

## 4. Diagnostico

### Onde ANANKE e claramente superior
1. **Qualidade de dados de execucao** — ex1k, mdq, quality tier. Ninguem no mercado tem isso.
2. **Decomposicao de custos** — 3 camadas (pf → npf → tnpf) vs numero unico.
3. **Analytics** — metrics view, time series, freq, dur, spread distribution.
4. **Freshness transparency** — AGE com 3 faixas + STALE badge.
5. **Custo** — $0 vs $1,188-$9,540/ano.

### Onde ArbitrageScanner e claramente superior
1. **Coverage** — 80+ CEX + 25+ DEX vs 6 CEX. Isso e o fator dominante.
2. **Tipos de arbitragem** — funding, futures, cross-chain, DEX.
3. **Info de rede/chain** — mostra qual blockchain, status por rede, filtro por rede.
4. **Filtros** — buy/sell separado, whitelist/blacklist, min/max lifetime, rede.
5. **Ecossistema** — wallet analysis, NFT, keyword monitoring, white-label.
6. **Acessibilidade** — SaaS sem setup tecnico.

### Gap critico que persiste no ANANKE
- **Rede de transferencia**: o user ve TF = checkmark mas nao sabe se e ERC20 ($30 gas) ou TRC20 ($1 gas). Isso afeta decisao de execucao diretamente.

### Gap parcialmente resolvido
- **Transfer feasibility (TF)**: adicionado ao frontend desde 13/Mar. Antes era 2/10, agora 4/10 (mostra status agregado, mas sem detalhamento por rede).

---

## 5. Conclusao Imparcial

ArbitrageScanner e uma plataforma **mais completa em escopo** — mais exchanges, mais tipos de arbitragem, mais ferramentas no ecossistema. Para um trader casual ou intermediario, e a escolha obvia.

ANANKE e **analiticamente superior no que faz** — a validacao via order book real (ex1k), a classificacao por quality tier, e a decomposicao de custos em 3 camadas sao features que nenhum scanner publico oferece. Para um trader profissional que opera nas 6 maiores exchanges e quer saber SE uma oportunidade e realmente executavel (nao apenas SE existe), ANANKE fornece dados que nao existem em nenhum outro lugar.

A equacao e: **breadth (ArbitrageScanner) vs depth (ANANKE)**.

O score ponderado favorece ArbitrageScanner (7.15 vs 6.45) principalmente por coverage de exchanges (peso 15%, 10 vs 3) — que e o maior diferencial numerico. Se restringirmos a analise apenas as 6 exchanges que ambos cobrem, ANANKE seria superior em quase todos os criterios de qualidade de dados.
