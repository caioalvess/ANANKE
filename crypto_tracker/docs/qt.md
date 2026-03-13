# Quality Tier (QT) — Classificacao de Qualidade da Oportunidade

## O que e

O Quality Tier e a nota que o ANANKE da pra cada oportunidade de arbitragem: A, B ou C. Ele classifica baseado em dados objetivos de execucao — nao em opiniao, nao em heuristica. E o resumo de uma pergunta: "essa oportunidade e executavel de verdade?"

## Por que existe

Uma tabela com 50 oportunidades e impossivel de processar sem priorizacao. Voce precisa saber em 1 segundo: o que merece atencao, o que precisa investigacao, e o que provavelmente nao presta.

O QT faz essa triagem automaticamente usando os unicos sinais objetivos disponiveis: o resultado da simulacao de execucao (ex1k), a profundidade do order book (mdq), e a frescura do dado (AGE).

## Os 3 tiers

### Tier A (qt = 1) — Execucao verificada

**Significado**: o depth probe confirmou que essa oportunidade da lucro executando $1K contra o order book real, E que o book tem liquidez suficiente.

**Criterios** (todos precisam ser verdadeiros):
- AGE <= 30 segundos (dado nao e stale)
- ex1k > 0 (simulacao de $1K e lucrativa)
- fill >= 50% (mdq >= 50% do ref_trade_size, ou seja, >= $500 de liquidez no book)

**Ordenacao**: por ex1k decrescente. O par com maior lucro real de execucao aparece primeiro.

**Acao**: e o sinal mais forte que o sistema pode dar. Se o AGE esta verde, pode agir.

### Tier B (qt = 2) — Nao verificado

**Significado**: o sistema nao tem dados de depth pra confirmar ou negar. O par esta fora do top-N monitorado pelo probe, ou o fill e insuficiente apesar de ex1k positivo.

**Criterios** (qualquer um):
- AGE <= 30s MAS sem dados de ex1k (par fora do top-N do probe)
- AGE <= 30s E ex1k > 0 MAS fill < 50% (book muito raso)

**Ordenacao**: por tnpf decrescente (no modo transfer) ou npf (no modo spot). Na ausencia de dados de depth, o lucro teorico apos custos e o melhor proxy.

**Acao**: nao ha confirmacao nem negacao. Vale investigacao — o spread pode ser real, voce so nao tem prova. Se o FREQ e alto e o msv e razoavel, pode valer a pena.

### Tier C (qt = 3) — Provavelmente inexecutavel

**Significado**: ou o depth probe provou que o spread nao sobrevive a execucao, ou o dado e velho demais pra confiar.

**Criterios** (qualquer um):
- AGE > 30 segundos (dado stale — preco pode ter mudado)
- ex1k <= 0 (simulacao provou prejuizo — slippage mata o spread)

**Ordenacao**: por tnpf decrescente. Mesmo sendo C, os com melhor tnpf aparecem primeiro — as vezes o dado fica stale por 31s e volta.

**Acao**: nao execute baseado no Tier C. Mas nao ignore completamente:
- Se ex1k <= 0 mas o spread e grande e o msv e alto, o book pode melhorar em segundos
- Se AGE > 30s, espere o dado atualizar — pode voltar pra A ou B

## Logica de decisao (fluxo)

```
AGE > 30s?
  └─ Sim → Tier C (dado stale)
  └─ Nao → Tem ex1k?
              └─ Nao → Tier B (sem dados de depth)
              └─ Sim → ex1k > 0?
                         └─ Nao → Tier C (execucao da prejuizo)
                         └─ Sim → fill >= 50%?
                                    └─ Nao → Tier B (liquidez insuficiente)
                                    └─ Sim → Tier A (verificado)
```

## Como ler no frontend

- **A (verde)**: verificado. Confiavel pra execucao.
- **B (neutro)**: incerto. Precisa de investigacao ou esperar o probe cobrir.
- **C (vermelho)**: problematico. Dado velho ou execucao comprovadamente ruim.

A tabela e ordenada primeiro por tier (A no topo, C embaixo) e depois pelo criterio de cada tier (ex1k pra A, tnpf pra B e C).

## Pra que serve na pratica

### Triagem instantanea

Abre a tela, olha o QT. Se nao tem nenhum A, nao tem nada confirmado pra executar agora. Se tem A, foca neles. Simples.

### Entender a cobertura do probe

Se voce ve muitos B e poucos A, significa que o depth probe esta cobrindo poucos pares (top-N e pequeno) ou que os pares cobertos nao estao passando. Pode ser sinal de mercado eficiente (spreads nao sobrevivem ao book) ou de configuracao conservadora.

### Monitorar transicoes

Um par que oscila entre A e B e um par onde o probe as vezes confirma e as vezes nao cobre. O FREQ alto nesses pares indica que vale a pena prestar atencao — quando o probe pegar e confirmar, e hora de agir.

Um par que era A e virou C provavelmente teve o book esvaziado ou o dado ficou stale. Se voltar a atualizar e o book se recompor, volta pra A.

## Combinacao com outras metricas

### QT + tnpf

- **Tier A + tnpf positivo**: execucao confirmada E lucro real apos todos os custos. Cenario de execucao.
- **Tier A + tnpf negativo**: o book aguenta mas os custos (fees + withdrawal) superam o spread. Pode ser viavel com trade size maior (a withdrawal fee se dilui).
- **Tier B + tnpf alto**: potencialmente lucrativo mas sem confirmacao de book. Investigar.

### QT + FREQ

- **Tier A + FREQ alta**: oportunidade verificada e recorrente. Candidato forte pra automacao.
- **Tier B + FREQ alta**: aparece muito mas o probe nao confirma. Ou esta fora do top-N, ou o book oscila. Vale monitorar.
- **Tier C + FREQ alta**: aparece muito mas nunca e executavel. Armadilha cronica.

### QT + DUR

- **Tier A + DUR longo**: verificado e sustentado. Se ninguem esta executando, investigue barreiras operacionais.
- **Tier C + DUR longo**: geralmente dado stale. O DUR esta contando mas a informacao nao esta atualizando.

### O cenario ideal

**Tier A + tnpf >= 0.5% + FREQ alta + DUR longo + mdq alto**: execucao verificada, lucro real generoso, recorrente, sustentado, com liquidez. E o sinal mais completo que o sistema pode dar.

## O que o QT NAO faz

- **Nao verifica withdrawal/deposit status**: um par Tier A pode ter withdrawal suspenso na exchange de compra. O ANANKE ainda nao exibe essa informacao no frontend (gap identificado na analise competitiva).
- **Nao prediz**: o QT e um snapshot. Tier A agora nao garante Tier A daqui a 5 segundos.
- **Nao esconde nada**: todos os tiers sao exibidos. O sistema informa; o trader decide.

## Onde fica no codigo

- Calculo: `src/ananke/web/server.py` — `_rank_arbitrage()`, linhas 193-247
- Criterio stale: `age_ms > 30_000` → Tier C
- Criterio depth: `ex1k > 0` E `fill >= 0.5` → Tier A; `ex1k <= 0` → Tier C; sem dados → Tier B
- Fill: `fill = min(1.0, (mdq or 0) / ref_trade_size)`
- Ordenacao: Tier A por ex1k, Tier B/C por tnpf (modo transfer) ou npf (modo spot)
- Frontend: coluna "QT" com letras A/B/C e cores verde/neutro/vermelho

## Diferencial competitivo

Nenhum scanner publico classifica oportunidades por qualidade de execucao verificada. A maioria ordena por spread bruto — que e o criterio menos confiavel. Os que tentam filtrar usam limites arbitrarios (ex: "minimo 0.5% profit") sem verificar se o book aguenta.

O QT do ANANKE e baseado em evidencia: o depth probe vai no order book, simula a execucao, mede a liquidez, e so entao atribui Tier A. E a diferenca entre "parece bom" e "foi testado e e bom".
