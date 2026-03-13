# MIN VOL (msv) — Volume Minimo Entre os Dois Lados

## O que e

O menor volume de negociacao em 24h (em USD) entre as duas exchanges do par de arbitragem. Se a Binance negociou $5M daquele token e a KuCoin negociou $200K, o msv e $200K — o lado mais fraco.

## Por que existe

Um spread de 2% nao serve pra nada se uma das exchanges mal negocia o token. Volume baixo significa:

- Order book raso (poucas ordens, gaps entre niveis)
- Slippage alto (seu trade move o preco)
- Risco de nao conseguir executar no tamanho que voce quer
- Precos potencialmente desatualizados (ninguem esta negociando pra manter o preco corrente)

O msv usa o lado mais fraco porque e ali que a execucao vai travar. Nao importa se a Binance negocia $500M/dia — se o outro lado tem $10K de volume, e isso que limita o trade.

## Como e calculado

```
msv = min(volume_quote_exchange_bid, volume_quote_exchange_ask)
```

Onde `volume_quote` e o volume de 24h em moeda de cotacao (USD/USDT) reportado pela exchange via API de tickers.

O msv e calculado em `src/ananke/web/server.py` — `_compute_arbitrage()`, usando o campo `volume_quote` do modelo Ticker.

## Como ler

- **msv > $1M**: volume saudavel nas duas exchanges. O par e negociado ativamente em ambos os lados.
- **msv $100K-$1M**: volume moderado. Funciona pra trades de $1-5K, mas nao espere liquidez infinita.
- **msv $10K-$100K**: volume baixo. O book provavelmente e raso. Trades de $1K ja podem causar impacto. Verifique ex1k e mdq antes de agir.
- **msv < $10K**: volume minimo. Alta chance de book vazio, spreads largos dentro da propria exchange, e precos desatualizados. Extreme cautela.

## Pra que serve na pratica

### Filtro de liquidez basico

O msv e o filtro mais grosseiro de liquidez. Antes de olhar ex1k e mdq (que sao snapshots pontuais do order book), o msv te da uma visao geral: esse token realmente negocia nas duas exchanges?

O sistema permite configurar um filtro de volume minimo (`min_volume_quote` na config). Tokens abaixo desse limiar nem aparecem nos resultados — sao eliminados antes do calculo de arbitragem.

### Contexto pra spreads grandes

Spreads enormes (5%, 10%, 50%) quase sempre aparecem em tokens com msv baixo. Isso nao significa que sao falsos — spreads grandes em tokens de baixa liquidez sao um fenomeno real em crypto (liquidez isolada, rede suspensa, exchange regional). Mas significa que a execucao e dificil e o risco e alto.

Quando voce ve um spread grande, olhe o msv primeiro:
- **Spread grande + msv alto**: raro e valioso. Investigue imediatamente.
- **Spread grande + msv baixo**: comum. E a realidade de tokens de nicho. Pode ser viavel com tamanho pequeno, mas nao escala.

### Complemento ao mdq

O mdq mede a liquidez disponivel agora no order book (snapshot pontual). O msv mede a atividade geral do token nas ultimas 24h (media historica). Os dois juntos dao perspectivas diferentes:

- **msv alto + mdq alto**: token liquido historicamente E agora. Confiavel.
- **msv alto + mdq baixo**: o token negocia bastante mas o book esta momentaneamente raso. Pode melhorar em minutos.
- **msv baixo + mdq alto**: raro. O book parece gordo mas o volume historico e baixo — pode ser uma unica ordem grande que vai sumir.
- **msv baixo + mdq baixo**: token iliquido. O spread existe justamente por isso.

## Combinacao com outras metricas

### msv + ex1k

- **msv alto + ex1k positivo**: volume historico confirma que o token e ativo, ex1k confirma que o book atual aguenta. Cenario forte.
- **msv baixo + ex1k positivo**: o book aguenta agora mas o volume geral e baixo. Funciona pra um trade, mas nao espere consistencia.
- **msv alto + ex1k negativo**: volume existe mas o book atual nao suporta execucao lucrativa. Slippage alto apesar de volume.

### msv + FREQ

- **msv alto + FREQ alta**: spread recorrente em token liquido. O melhor perfil pra operacao consistente.
- **msv baixo + FREQ alta**: spread recorrente em token iliquido. Armadilha classica — o spread existe justamente porque ninguem consegue executar com facilidade.

### msv + DUR

- **msv baixo + DUR longo**: explica por que ninguem fecha o spread — nao tem liquidez. O spread fica ali porque executar e dificil.
- **msv alto + DUR longo**: spread persistente em token liquido. Investigue — pode haver barreira operacional (withdrawal suspenso).

## Relacao com o Quality Tier

O msv nao entra diretamente no calculo do Quality Tier. O tier usa ex1k e mdq (dados pontuais do order book). Mas tokens com msv muito baixo raramente chegam a Tier A porque o book tende a ser raso demais.

O msv funciona como pre-filtro: se o volume minimo configurado elimina o token antes do calculo de arbitragem, ele nunca chega a ser avaliado pelo tier.

## Onde fica no codigo

- Pre-filtro: `src/ananke/web/server.py` — `_compute_arbitrage()`, linhas 73/81: tokens com `volume_quote < min_vol` sao eliminados
- Calculo: `src/ananke/web/server.py` — `_compute_arbitrage()`, linha 168: `msv = min(max_bid_vol, min_ask_vol)`
- Dados: `src/ananke/models.py` — `Ticker.volume_quote` (volume 24h em quote currency)
- Frontend: coluna "MIN VOL" na tabela de arbitragem
- Filtro frontend: campo "MIN VOL $" permite filtrar visualmente por volume minimo

## Diferencial competitivo

A maioria dos scanners mostra volume de 24h como metrica de liquidez. O ANANKE tambem mostra msv mas vai alem com ex1k e mdq — que medem a liquidez real disponivel no order book agora, nao a media historica. O msv e o contexto macro; ex1k e mdq sao a verificacao micro.
