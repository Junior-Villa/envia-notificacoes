# Prometheus → Discord Alert Service

Serviço em Python que coleta métricas do Prometheus e envia alertas inteligentes para o Discord via Webhook.

Focado em aplicações **Java / Spring Boot**, utilizando métricas padrão como `http_server_requests_seconds_count` e `jvm_memory_*`.

---

# Objetivo

Reduzir ruído em alertas e fornecer informações acionáveis, com:

* Detecção de anomalias em tempo quase real
* Agrupamento inteligente de erros
* Contexto adicional (ex: volume histórico de requisições)
* Controle de repetição de alertas (cooldown + assinatura)

---

# Funcionalidades

## Alto volume de requisições

* Detecta endpoints com taxa acima do threshold (req/s)
* Baseado em `rate()` no Prometheus

---

## Erros 4xx (cliente)

* Calcula:

  * Taxa de erro por endpoint
  * Taxa total de requisições
* Gera alerta se:

  * Ratio ≥ threshold
  * E taxa absoluta ≥ threshold
* Agrupa por status (400, 401, 404...)

---

## Erros 5xx (servidor)

* Mesma lógica dos 4xx
* Foco em falhas críticas (500, 502, 503...)

---

## Consumo de Heap

* Usa:

```
jvm_memory_used_bytes / jvm_memory_max_bytes
```

* Alerta baseado em % de uso

---

### 📊 Enriquecimento com histórico

* Cada endpoint inclui:

  * Total de requisições nos últimos 5 dias, podendo ser ajustado conforme necessidade
* Ajuda a entender impacto real do problema

---

### 🔁 Controle de alertas

Evita spam com:

* Cooldown por tipo + endpoint/app
* Assinatura de estado (só alerta se algo mudou)

---

## 🐳 Como rodar com Docker

### 1. Clone o projeto

```
git clone https://github.com/Junior-Villa/envia-notificacoes.git
cd envia-notificacoes
```

### 2. Configure o `.env`

Exemplo:

```
PROMETHEUS_URL=https://seu-prometheus.com
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx

APP_NAMES=api-1,api-2

REQ_RATE_THRESHOLD=1000
ERROR_4XX_RATIO_THRESHOLD=0.05
ERROR_5XX_RATIO_THRESHOLD=0.01
HEAP_USAGE_THRESHOLD=80

POLL_INTERVAL_SECONDS=300
ALERT_COOLDOWN_SECONDS=300
```

---

### 3. Suba o container

```
docker-compose up -d
```

---

## 🧪 Estrutura do Projeto

```
.
├── app.py              # Lógica principal
├── requirements.txt   # Dependências
├── Dockerfile
├── docker-compose.yml
└── .env
```

---

## 📡 Métricas utilizadas

* `http_server_requests_seconds_count`
* `jvm_memory_used_bytes`
* `jvm_memory_max_bytes`

Compatível com aplicações Spring Boot com Actuator + Micrometer.

---

## 🔧 Customização

Tudo via ENV:

| Variável                  | Descrição           |
| ------------------------- | ------------------- |
| PROMETHEUS_URL            | URL do Prometheus   |
| DISCORD_WEBHOOK_URL       | Webhook do Discord  |
| APP_NAMES                 | Lista de apps       |
| REQ_RATE_THRESHOLD        | Threshold req/s     |
| ERROR_4XX_RATIO_THRESHOLD | % erro 4xx          |
| ERROR_5XX_RATIO_THRESHOLD | % erro 5xx          |
| HEAP_USAGE_THRESHOLD      | % heap              |
| POLL_INTERVAL_SECONDS     | Intervalo de coleta |
| ALERT_COOLDOWN_SECONDS    | Tempo entre alertas |

---
