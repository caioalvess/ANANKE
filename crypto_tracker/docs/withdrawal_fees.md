# Withdrawal Fees — Taxas de Envio por Exchange

## O que sao

Withdrawal fees sao taxas fixas que cada exchange cobra pra voce transferir um ativo pra fora dela. Diferente das taker fees (percentuais), as withdrawal fees sao um valor fixo em unidades do ativo — por exemplo, 0.0001 BTC, independente de quanto voce esta transferindo.

Em arbitragem de transferencia, apos comprar na exchange barata, voce precisa enviar o ativo pra exchange cara pra vender. Essa transferencia tem custo. E esse custo que transforma o npf em tnpf.

## Como o ANANKE obtem as withdrawal fees

O sistema nao usa valores fixos hardcoded — ele consulta as APIs das exchanges diretamente pra obter as fees reais e atualizadas. Os dados sao cacheados por 24 horas.

### Fontes de dados (por prioridade)

| Fonte | O que fornece | Requer API key? |
|-------|---------------|-----------------|
| KuCoin `/api/v3/currencies` | Withdrawal fees por chain + status deposit/withdraw | Nao (publica) |
| OKX `/api/v5/asset/currencies` | Withdrawal fees por chain + status deposit/withdraw | Sim |
| Binance `/sapi/v1/capital/config/getall` | Withdrawal fees por network + status deposit/withdraw | Sim |
| Bybit `/v5/asset/coin/query-info` | Withdrawal fees por chain + status deposit/withdraw | Sim |
| Gate.io `/api/v4/spot/currencies` | **Apenas** status deposit/withdraw (sem fees) | Nao (publica) |
| Kraken `/0/public/Assets` | **Apenas** status deposit/withdraw (sem fees) | Nao (publica) |
| withdrawalfees.com | Fallback: fee minima por ativo (~32 paginas) | Nao |

### Cadeia de fallback pra cada ativo

Quando o sistema precisa da withdrawal fee de um ativo, a busca segue esta ordem:

1. **Fee especifica da exchange**: se existe fee de (Exchange, SYMBOL) coletada da API daquela exchange, usa essa
2. **Fallback generico**: se nao existe fee especifica, usa o fallback generico do ativo (merge de KuCoin > OKX > Binance > Bybit)
3. **withdrawalfees.com**: preenche gaps de ativos que nenhuma exchange retornou
4. **Zero**: se nenhuma fonte tem dado, assume fee = 0 (o tnpf fica igual ao npf)

### Rede mais barata

Para cada ativo, o sistema pega a **rede mais barata disponivel** (a com menor fee entre as chains habilitadas). Isso e feito automaticamente — se BTC pode ser sacado via Lightning (fee quase zero) ou via Bitcoin mainnet (fee mais alta), o sistema usa a menor.

### Quais exchanges fornecem fees diretas

| Exchange | Fornece withdrawal fees? | Fornece status deposit/withdraw? |
|----------|--------------------------|----------------------------------|
| KuCoin   | Sim (per-chain)          | Sim |
| OKX      | Sim (per-chain)          | Sim |
| Binance  | Sim (per-network)        | Sim |
| Bybit    | Sim (per-chain)          | Sim |
| Gate.io  | **Nao**                  | Sim |
| Kraken   | **Nao**                  | Sim |

**Gate.io e Kraken** nao fornecem withdrawal fees via API publica. Pra ativos nessas exchanges, o sistema usa o fallback (fee de outra exchange ou withdrawalfees.com). Na pratica isso e uma boa aproximacao porque as withdrawal fees de um mesmo ativo tendem a ser similares entre exchanges (a fee vai pra rede, nao pra exchange).

## Caracteristicas das withdrawal fees

### Custo fixo, nao percentual

A withdrawal fee e a mesma se voce transfere $100 ou $100.000. Isso tem consequencias:

```
Trade de $1K com wf de $6:  impacto = 0.6%
Trade de $5K com wf de $6:  impacto = 0.12%
Trade de $10K com wf de $6: impacto = 0.06%
```

Quanto maior o trade, menor o impacto percentual. O tnpf e calculado com ref_trade_size de $1K — se voce opera tamanho maior, o impacto real e menor.

### Varia por ativo e por rede

Cada ativo tem sua propria fee, e a mesma moeda pode ter fees drasticamente diferentes dependendo da rede:

Exemplos tipicos (valores aproximados, variam por exchange):

| Ativo | Rede barata | Fee | Rede cara | Fee |
|-------|-------------|-----|-----------|-----|
| BTC   | Lightning   | ~0 sat | Bitcoin mainnet | 0.0001-0.0005 BTC |
| ETH   | Arbitrum    | ~0.0001 ETH | Ethereum L1 | 0.001-0.005 ETH |
| USDT  | TRC20       | ~1 USDT | ERC20 | 5-25 USDT |

O ANANKE usa automaticamente a rede com menor fee. Mas na pratica, ao executar, confirme que a exchange de destino aceita deposito pela mesma rede barata — senao voce sera forcado a usar uma rede mais cara.

### Pode mudar sem aviso

Exchanges ajustam withdrawal fees com frequencia, especialmente em periodos de congestionamento de rede. O cache de 24h do ANANKE mitiga isso mas nao elimina — a fee real no momento da execucao pode ser diferente.

## Como entra no calculo

A withdrawal fee alimenta a coluna TAXA ENVIO (wf) e o calculo do tnpf:

```
wf   = withdrawal_fee_em_base * preco_bid
tnpf = npf - (wf / ref_trade_size) * 100
```

A withdrawal fee e cobrada da exchange de **compra** (ask exchange) — porque e de la que voce precisa sacar o ativo pra enviar pra exchange de venda.

## Particularidades por exchange

### Binance
- API retorna fees por network com campo `withdrawFee`
- Indica se cada network esta habilitada pra withdraw/deposit
- Requer API key (ANANKE_BINANCE_API_KEY + ANANKE_BINANCE_API_SECRET)
- Tem rede interna (Binance Chain) com fees muito baixas, mas so funciona entre contas Binance

### Bybit
- API retorna fees por chain com campo `withdrawFee`
- Indica status de cada chain individualmente
- Requer API key (ANANKE_BYBIT_API_KEY + ANANKE_BYBIT_API_SECRET)

### KuCoin
- API publica (sem key), principal fonte de fallback
- Retorna `withdrawalMinFee` por chain
- Prioridade mais alta no merge de fallback — quando nenhuma exchange tem fee especifica, a fee da KuCoin e usada

### OKX
- API retorna fees por chain com campo `minFee`
- Requer API key com assinatura HMAC (ANANKE_OKX_API_KEY + ANANKE_OKX_API_SECRET + ANANKE_OKX_PASSPHRASE)

### Gate.io
- API publica retorna apenas status de deposit/withdraw (habilitado/desabilitado)
- **Nao fornece valores de withdrawal fee via API**
- Fees vem do fallback (KuCoin/OKX/Binance/Bybit ou withdrawalfees.com)
- Na pratica, a fee real da Gate.io para o mesmo ativo/rede tende a ser similar a de outras exchanges

### Kraken
- API publica retorna apenas status (enabled/deposit_only/withdrawal_only/disabled)
- **Nao fornece valores de withdrawal fee via API**
- Fees vem do fallback
- Kraken tem fama de fees de withdrawal mais altas que a media — o fallback pode subestimar

## Status de deposit/withdraw

Alem das fees, o sistema coleta se deposit e withdraw estao habilitados em cada exchange. Isso e critico:

- **Withdraw bloqueado** na exchange de compra: voce compra mas nao consegue enviar pra outra exchange. O trade fica travado.
- **Deposit bloqueado** na exchange de venda: voce envia mas a exchange nao credita. Mesmo efeito.

Esses status sao coletados de todas as 6 exchanges e estao disponiveis no backend (FeeRegistry), mas **ainda nao sao exibidos no frontend** — gap identificado na analise competitiva.

## Como reduzir custos de withdrawal

### 1. Escolher rede mais barata
Ao executar o saque, selecione a rede com menor fee que ambas as exchanges suportem. O ANANKE calcula com a rede mais barata da exchange de origem, mas a exchange de destino precisa aceitar deposito por ela.

### 2. Pre-posicionar capital
Mantenha saldo nas duas exchanges. Em vez de comprar → transferir → vender, voce compra numa e vende na outra simultaneamente. Elimina a withdrawal fee completamente. Requer capital nas duas pontas e rebalanceamento periodico.

### 3. Operar tamanho maior
A fee e fixa. Em $1K o impacto e grande; em $10K e diluido. Se um par tem npf bom mas tnpf ruim por causa do wf, aumente o tamanho (respeitando o mdq).

### 4. Usar transferencia interna
Algumas exchanges permitem transferencia interna (ex: Binance → Binance) sem fee. Nao se aplica a arbitragem cross-exchange, mas e util pra rebalanceamento entre subcontas.

## Onde fica no codigo

- Fontes: `src/ananke/fee_registry.py` — `_fetch_kucoin_currency_data()`, `_fetch_okx_currency_data()`, `_fetch_binance_currency_data()`, `_fetch_bybit_currency_data()`
- Status: `src/ananke/fee_registry.py` — `_fetch_gateio_transfer_status()`, `_fetch_kraken_transfer_status()`
- Fallback: `src/ananke/fee_registry.py` — `_fetch_wfees_fallback()` (withdrawalfees.com)
- Lookup: `src/ananke/fee_registry.py` — `FeeRegistry.withdrawal_fee()` com fallback chain
- Conversao USD: `src/ananke/fee_registry.py` — `FeeRegistry.withdrawal_cost_quote()`
- Uso no tnpf: `src/ananke/web/server.py` — `_compute_arbitrage()`, linhas 152-162
- Cache: 24 horas, em `~/.ananke/fee_cache.json`
- Builder: `src/ananke/fee_registry.py` — `build_fee_registry()`, orquestra todas as fontes
