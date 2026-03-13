# AGE — Freshness do Dado de Preco

## O que e

Ha quanto tempo o dado de preco foi atualizado pela ultima vez. Mede a idade do ticker mais antigo entre os dois lados da oportunidade de arbitragem.

## Por que existe

O ANANKE recebe precos via WebSocket em tempo real. Cada ticker chega com um timestamp de quando a exchange gerou aquele preco.

Se BTC na Binance mostra bid de $100K mas o AGE e 45 segundos, aquele preco provavelmente ja nao existe mais. O book ja mudou, o spread que aparece na tela e fantasma.

## Como e calculado

O arb tem dois lados: ask (compra) e bid (venda). Cada lado tem seu timestamp. O AGE pega o pior dos dois:

```
age = max(agora - timestamp_bid, agora - timestamp_ask)
```

Se um lado ta fresco (2s) mas o outro ta velho (15s), o AGE mostra 15s — porque a oportunidade so e real se os dois lados estiverem atuais.

O calculo acontece em duas etapas:
1. No servidor (`_compute_arbitrage`): calcula `age` no momento do broadcast
2. No frontend: recalcula usando `server_ts` para manter o valor atualizado entre broadcasts

## Como ler

- **Verde (<3s)** — dado fresco, preco confiavel
- **Amarelo (3-10s)** — aceitavel, mas fique atento
- **Vermelho (>10s)** — preco possivelmente desatualizado
- **Badge STALE (>30s)** — linha com 40% de opacidade e borda vermelha

## Pra que serve na pratica

Arbitragem spot e uma corrida contra o tempo. O spread aparece e desaparece em segundos. O AGE te diz se vale a pena agir ou nao.

Se voce ve uma oportunidade de 1.2% mas o AGE ta em 12 segundos, aquele preco ja andou. Voce vai abrir a exchange, mandar a ordem, e o preco que te fez clicar ja nao existe. Perdeu tempo, ou pior — executou num preco pior e tomou prejuizo.

### Regra operacional por faixa

- **Verde (<3s)**: pode agir. O preco na tela e o que voce vai encontrar na exchange agora.
- **Amarelo (3-10s)**: cautela. Se o spread for gordo (>2%), ainda vale. Se for apertado (<0.5%), provavelmente ja fechou.
- **Vermelho (>10s)**: nao aja baseado nesse preco. Use como referencia de que aquele par TEM oportunidades recorrentes — monitore pra pegar fresco.
- **STALE (>30s)**: ignore. O sistema rebaixa pra Tier C automaticamente. Ta ali so pra registrar que existiu.

### Combinacao com FREQ e DUR

O AGE ganha mais poder quando combinado com as metricas de historico:

- Par com **FREQ alta** (aparece frequentemente) + **DUR longo** (fica ativo por bastante tempo) + **AGE vermelho** → a oportunidade volta, vale ficar de olho e esperar o dado ficar verde pra agir
- Par com **FREQ baixa** + **AGE verde** → oportunidade rara e fresca, pode valer a pena agir rapido
- Par com **FREQ alta** + **AGE verde** + **Tier A** → cenario ideal: oportunidade recorrente, dado fresco, execucao confirmada pelo depth probe

## Relacao com o Quality Tier

Dados com age > 30 segundos recebem automaticamente **Tier C** (non-executable), independente do ex1k ou mdq. O ranker em `_rank_arbitrage()` checa staleness antes de qualquer outra coisa:

```python
if age_ms > 30_000:
    r["qt"] = 3  # Tier C
    continue
```

A logica: se o preco tem mais de 30s, a oportunidade quase certamente ja nao existe nos valores mostrados. Nao importa se o depth probe confirmou execucao — os precos mudaram.

## O que causa AGE alto

- **Exchange com WebSocket instavel** — desconexoes temporarias fazem o ultimo preco ficar congelado
- **Par de baixa liquidez** — poucos trades acontecem, o ticker nao atualiza com frequencia
- **Problema de rede** — latencia entre o ANANKE e a exchange

## Onde fica no codigo

- Calculo servidor: `src/ananke/web/server.py` — `_compute_arbitrage()`, campo `age` no resultado
- Tier assignment: `src/ananke/web/server.py` — `_rank_arbitrage()`, check `age_ms > 30_000`
- Frontend: coluna "AGE" na tabela de arbitragem, classes CSS `age-fresh`, `age-warn`, `age-old`, `stale-label`
- Recalculo frontend: `arbAge = serverTs ? Math.max(serverTs - o.bts, serverTs - o.ats) : o.age`

## Diferencial competitivo

A maioria dos scanners nao mostra freshness granular. ArbitrageScanner tem "pair lifetime" (quanto tempo o spread existe), que e um conceito diferente — mede duracao da oportunidade, nao idade do dado. O AGE do ANANKE mede confiabilidade do preco em si.

CryptoArbitrageScreener atualiza a cada 15 minutos — todo dado deles tem "AGE" implicito de ate 15 minutos. O ANANKE atualiza a cada ~1s via WebSocket.
