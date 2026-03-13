# DUR — Duracao Continua da Oportunidade

## O que e

Ha quanto tempo aquela oportunidade de arbitragem esta continuamente ativa, em segundos. Mede a sequencia atual sem interrupcao.

## Por que existe

Saber que um spread existe agora nao e suficiente. Voce precisa saber ha quanto tempo ele esta ali — isso muda o significado da oportunidade e a urgencia da acao.

Um spread que acabou de aparecer (DUR = 2s) pode ser um flash que vai sumir. Um spread que esta ali ha 3 minutos (DUR = 180s) e algo estrutural que ninguem esta fechando — e ai voce precisa entender por que.

## Diferenca entre DUR e FREQ

Os dois medem "persistencia" mas de formas diferentes:

- **FREQ**: conta quantas vezes o par apareceu nos ultimos 5 minutos. Se o spread apareceu, sumiu e voltou 50 vezes, FREQ = 50.
- **DUR**: mede a sequencia atual sem interrupcao. Se o spread sumiu por 1 segundo, o DUR resetou pra zero naquele momento e comecou a contar de novo.

FREQ e o historico recente. DUR e o agora.

Exemplo: um par com FREQ = 200 e DUR = 5s significa que apareceu 200 vezes nos ultimos 5 minutos, mas a sessao atual so tem 5 segundos — ele voltou agora. Ja um par com FREQ = 50 e DUR = 120s significa que apareceu menos vezes no total mas a sessao atual esta ininterrupta ha 2 minutos.

## Como ler

- **DUR > 120s**: spread aberto ha mais de 2 minutos sem interrupcao. Ninguem executou ou o volume de execucao nao foi suficiente pra fechar o gap. Investigue por que.
- **DUR 30-120s**: spread sustentado. Tempo suficiente pra avaliar e agir se as outras metricas confirmarem.
- **DUR 10-30s**: spread recente. Pode estar se formando ou prestes a fechar. Aja rapido se for agir.
- **DUR < 10s**: acabou de aparecer. Ou vai fechar rapido (alguem vai executar) ou vai crescer (divergencia em andamento).
- **DUR = 0**: o par nao esta ativo agora. Pode ter estado ha pouco — FREQ > 0 confirma.

## Pra que serve na pratica

O DUR e o sinal de urgencia e de diagnostico. Ele responde duas perguntas:

### 1. Quanto tempo eu ainda tenho?

Se DUR esta crescendo, o spread esta aberto e ninguem esta fechando. Voce tem tempo. Se DUR e baixo e o FREQ mostra que o par aparece e some rapido, voce tem segundos.

### 2. Por que ninguem esta executando?

DUR longo em spread grande e um sinal de alerta. Se o spread e 2%, o ex1k e positivo, o mdq e alto e mesmo assim ninguem fecha ha 3 minutos — tem algo errado que nao esta visivel nos numeros. Causas comuns:

- **Withdrawal suspenso**: a exchange bloqueou saques daquele token. O spread existe mas ninguem consegue rebalancear. Esse e o gap mais critico que o ANANKE ainda nao exibe no frontend.
- **Deposito suspenso**: mesmo problema no lado oposto.
- **Rede congestionada**: transferencia demora horas, o risco de preco durante a transferencia come o lucro.
- **Par de nicho**: pouca gente monitora. Oportunidade genuina que o mercado esta ignorando.
- **Spread pequeno demais**: 0.3% nao compensa o trabalho manual pra maioria dos traders. Mas compensa se automatizado.

## Combinacao com outras metricas

### DUR + FREQ

- **DUR longo + FREQ alta**: o spread esta ali agora E aparece frequentemente. Oportunidade estavel e recorrente. A mais confiavel pra operar.
- **DUR longo + FREQ baixa**: o spread esta ali agora mas e raro. Pode ser uma situacao atipica (manutencao de exchange, evento de mercado). Investigue antes de agir.
- **DUR curto + FREQ alta**: o spread fica aparecendo e sendo fechado. Mercado eficiente naquele par. Pra capturar, precisa de velocidade (automacao).
- **DUR curto + FREQ baixa**: flash raro. Nao planeje em cima disso.

### DUR + AGE

- **DUR longo + AGE verde**: spread aberto ha tempo E dado fresco. Confiavel.
- **DUR longo + AGE vermelho**: contradiz. Se o DUR e longo mas o AGE esta velho, significa que o dado parou de atualizar. O spread pode ter fechado e voce nao sabe. Nao confie.
- **DUR curto + AGE verde**: acabou de aparecer e esta fresco. Se for agir, e agora.

### DUR + ex1k

- **DUR longo + ex1k positivo**: spread aberto ha tempo E confirmado pelo book. Se o AGE tambem esta verde, e dinheiro na mesa. A pergunta e: por que ninguem esta pegando?
- **DUR longo + ex1k negativo**: o spread existe mas o book nao aguenta execucao. Isso explica por que ninguem esta fechando — quem olha o book ve que nao da.
- **DUR longo + ex1k = traco**: spread persistente fora do top-N do probe. Vale investigacao manual.

### DUR + mdq

- **DUR longo + mdq alto**: spread duradouro com liquidez. Se ex1k tambem e positivo, e o cenario mais forte — oportunidade real que escala.
- **DUR longo + mdq baixo**: spread duradouro mas sem liquidez. Explica a persistencia: ninguem fecha porque nao da pra colocar tamanho.

### O cenario ideal

**DUR longo + FREQ alta + AGE verde + Tier A + mdq alto**: spread ininterrupto, recorrente, fresco, confirmado por depth, com liquidez. Nao existe sinal mais forte que esse.

### O sinal de alerta

**DUR muito longo (>300s) + spread grande (>2%) + Tier A**: se tudo parece perfeito mas ninguem executa ha 5 minutos, desconfie. Provavelmente ha uma barreira operacional que o ANANKE ainda nao exibe (withdrawal suspenso, rede congestionada).

## Relacao com o Quality Tier

O DUR nao entra diretamente no calculo do Quality Tier. Mas na pratica:

- Tier A com DUR longo e o sinal mais forte do sistema
- Tier C com DUR longo geralmente significa dado stale (AGE > 30s) — o DUR esta contando mas o dado nao esta atualizando

## Onde fica no codigo

- Tracking: `src/ananke/metrics.py` — `MetricsCollector._active_since` registra quando cada par apareceu pela primeira vez na sequencia atual
- Reset: `src/ananke/metrics.py` — `MetricsCollector.record()` remove de `_active_since` os pares que sumiram do snapshot
- Calculo: `src/ananke/metrics.py` — `MetricsCollector.get_active_duration()` retorna `now - first_seen`
- Enriquecimento: `src/ananke/metrics.py` — `MetricsCollector.enrich_arb_results()` adiciona `dur` ao resultado
- Frontend: coluna "DUR" na tabela de arbitragem

## Diferencial competitivo

ArbitrageScanner tem "pair lifetime" que e o conceito mais proximo — mede ha quanto tempo o spread existe. A diferenca e que o DUR do ANANKE trabalha em conjunto com o FREQ, dando duas dimensoes de persistencia (historica e atual) em vez de uma so. Nenhum outro scanner publico oferece essa combinacao.
