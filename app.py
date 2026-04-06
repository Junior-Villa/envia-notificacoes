import os
import time
import logging
from typing import Dict, Any, Tuple, List, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =======================
# Configuração via ENV
# =======================

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "https://prometheus.waybe.com.br")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Pode usar APP_NAMES=api1,api2,api3
# Se quiser manter só 1, pode continuar usando APP_NAME=...
_raw_apps = os.getenv("APP_NAMES") or os.getenv("APP_NAME", "")
APP_NAMES: List[str] = [a.strip() for a in _raw_apps.split(",") if a.strip()]

# Thresholds (podem ser ajustados via ENV)
REQ_RATE_THRESHOLD = float(os.getenv("REQ_RATE_THRESHOLD", "1000"))  # req/s por endpoint
ERROR_4XX_RATIO_THRESHOLD = float(os.getenv("ERROR_4XX_RATIO_THRESHOLD", "0.05"))  # 5%
ERROR_5XX_RATIO_THRESHOLD = float(os.getenv("ERROR_5XX_RATIO_THRESHOLD", "0.01"))  # 1%
HEAP_USAGE_THRESHOLD = float(os.getenv("HEAP_USAGE_THRESHOLD", "80"))  # %
ERROR_4XX_ABS_RATE_THRESHOLD = float(os.getenv("ERROR_4XX_ABS_RATE_THRESHOLD", "0.01"))
ERROR_5XX_ABS_RATE_THRESHOLD = float(os.getenv("ERROR_5XX_ABS_RATE_THRESHOLD", "0.01"))

# Frequência de checagem / repetição
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 3 min
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))  # 3 min

# SSL do Prometheus (caso tenha cert self-signed, setar PROMETHEUS_VERIFY_SSL=false)
session = requests.Session()
session.verify = os.getenv("PROMETHEUS_VERIFY_SSL", "true").lower() == "true"

# (tipo_alerta, chave) -> timestamp último envio
# chave inclui app quando fizer sentido
alert_state: Dict[Tuple[str, str], float] = {}

# (tipo_alerta, chave) -> última "assinatura" (resumo dos dados enviados)
alert_last_signature: Dict[Tuple[str, str], str] = {}


# =======================
# Funções utilitárias
# =======================

def query_prometheus(promql: str) -> Dict[str, Any]:
    url = f"{PROMETHEUS_URL.rstrip('/')}/api/v1/query"
    try:
        resp = session.get(url, params={"query": promql}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            logging.error("Prometheus retornou status != success: %s", data)
            return {}
        return data.get("data", {})
    except Exception as e:
        logging.error("Erro ao consultar Prometheus: %s", e)
        return {}


def get_req_count_last_5d_by_uri(app_name: str) -> Dict[str, int]:
    """
    Retorna dict: uri -> total de requisições nos últimos 5 dias.
    Usa increase() pois http_server_requests_seconds_count é counter.
    """
    promql = (
        'sum by(uri) ('
        f'  increase(http_server_requests_seconds_count{{application="{app_name}", uri!="/actuator/prometheus", uri!="/**"}}[5d])'
        ')'
    )

    data = query_prometheus(promql)
    results = data.get("result", [])

    counts: Dict[str, int] = {}
    for s in results:
        metric = s.get("metric", {})
        uri = metric.get("uri")
        if not uri:
            continue

        value_str = s.get("value", [0, "0"])[1]
        try:
            counts[uri] = int(float(value_str))
        except ValueError:
            continue

    return counts

def format_human_number(value: int) -> str:
    """
    Formata números grandes para formato humano:
    1_200      -> 1.2K
    3_450_000  -> 3.45M
    1_100_000_000 -> 1.1B
    """
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "0"

    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"

    return str(int(value))


def should_send_alert(alert_type: str, key: str, signature: Optional[str] = None) -> bool:
    """
    Retorna True se deve enviar alerta.

    Critérios:
      - Se a assinatura (signature) for igual à última enviada para (tipo + chave),
        NÃO envia (nada mudou).
      - Caso contrário, aplica cooldown por (tipo + chave).
    """
    now = time.time()
    state_key = (alert_type, key)

    # Se temos assinatura e ela é idêntica à última, não reenviamos
    if signature is not None:
        last_sig = alert_last_signature.get(state_key)
        if last_sig == signature:
            return False

    last_time = alert_state.get(state_key)
    if last_time is not None and (now - last_time) < ALERT_COOLDOWN_SECONDS:
        return False

    # Atualiza estado de envio
    alert_state[state_key] = now
    if signature is not None:
        alert_last_signature[state_key] = signature

    return True


def send_discord_alert(title: str, description: str, color: int = 15158332) -> None:
    """
    Envia um embed simples para o Webhook do Discord.
    """
    if not DISCORD_WEBHOOK_URL:
        logging.error("DISCORD_WEBHOOK_URL não configurada")
        return

    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
            }
        ]
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            logging.error(
                "Falha ao enviar alerta para Discord: %s - %s",
                resp.status_code,
                resp.text,
            )
    except Exception as e:
        logging.error("Erro ao enviar alerta para Discord: %s", e)


# =======================
# Checks de métricas
# =======================

def check_high_request_rate_for_app(app_name: str):
    """
    Painel tiver quantidade de request acima de REQ_RATE_THRESHOLD (req/s por endpoint).
    Usa rate em 10m, por uri.
    Continua 1 alerta por endpoint.
    """
    promql = (
        'sum by(uri) ('
        f'  rate(http_server_requests_seconds_count{{application="{app_name}", uri!="/actuator/prometheus", uri!="/**"}}[10m])'
        ')'
    )

    data = query_prometheus(promql)
    results = data.get("result", [])

    for series in results:
        metric = series.get("metric", {})
        uri = metric.get("uri", "<desconhecido>")
        value_str = series.get("value", [0, "0"])[1]

        try:
            value = float(value_str)  # req/s
        except ValueError:
            continue

        if value >= REQ_RATE_THRESHOLD:
            alert_key = f"{app_name}:req_rate:{uri}"
            signature = f"{round(value, 2)}"
            if should_send_alert("high_request_rate", alert_key, signature):
                title = "🚨 Alto volume de requisições"
                description = (
                    f"Aplicação: `{app_name}`\n"
                    f"Endpoint: `{uri}`\n"
                    f"Taxa média de requisições: **{value:.2f} req/s**\n"
                    f"Limiar configurado: **{REQ_RATE_THRESHOLD:.2f} req/s**\n"
                    f"Fonte: Prometheus ({PROMETHEUS_URL})"
                )
                send_discord_alert(title, description, color=15105570)


def check_error_4xx_ratio_for_app(app_name: str):
    """
    Alerta de erros 4xx agrupado por app.
    - Calcula rate de erros e total em 10m
    - Faz a razão em Python
    - Usa 2 critérios:
      * razão >= ERROR_4XX_RATIO_THRESHOLD
      * e taxa de erros >= ERROR_4XX_ABS_RATE_THRESHOLD
    Agrupa endpoints por status (400, 401, 403, 404...).

    Melhoria:
    - Adiciona no card o total de requisições do endpoint nos últimos 5 dias (req/5d).
    """

    # (NOVO) total de requests por endpoint nos últimos 5 dias (para enriquecer o card)
    req_5d_by_uri = get_req_count_last_5d_by_uri(app_name)

    # rate de erros 4xx por uri,status
    errors_promql = (
        'sum by(uri, status) ('
        f'  rate(http_server_requests_seconds_count{{application="{app_name}", status=~"4..", uri!="/actuator/prometheus", uri!="/**"}}[10m])'
        ')'
    )

    # rate total por uri
    total_promql = (
        'sum by(uri) ('
        f'  rate(http_server_requests_seconds_count{{application="{app_name}", uri!="/actuator/prometheus", uri!="/**"}}[10m])'
        ')'
    )

    errors_data = query_prometheus(errors_promql)
    total_data = query_prometheus(total_promql)

    errors_results = errors_data.get("result", [])
    total_results = total_data.get("result", [])

    # monta dict: uri -> total_rate
    total_by_uri: Dict[str, float] = {}
    for s in total_results:
        metric = s.get("metric", {})
        uri = metric.get("uri")
        if not uri:
            continue
        v_str = s.get("value", [0, "0"])[1]
        try:
            total_by_uri[uri] = float(v_str)
        except ValueError:
            continue

    # status_code -> list[(uri, ratio)]
    status_map: Dict[str, List[Tuple[str, float]]] = {}

    for s in errors_results:
        metric = s.get("metric", {})
        uri = metric.get("uri")
        status = metric.get("status", "4xx")

        if not uri:
            uri = "<desconhecido>"

        err_str = s.get("value", [0, "0"])[1]
        try:
            err_rate = float(err_str)  # req/s de erro
        except ValueError:
            continue

        total_rate = total_by_uri.get(uri, 0.0)

        # evita divisão bizarra; se total for zero, usa total = err_rate
        denom = total_rate if total_rate > 0 else err_rate
        if denom <= 0:
            continue

        ratio = err_rate / denom  # 0..1

        # critérios: razão + taxa absoluta
        if ratio >= ERROR_4XX_RATIO_THRESHOLD and err_rate >= ERROR_4XX_ABS_RATE_THRESHOLD:
            status_map.setdefault(status, []).append((uri, ratio))

    if not status_map:
        return

    alert_key = f"{app_name}:4xx_ratio"

    # monta assinatura: status|uri|percent ordenados
    sig_parts: List[str] = []
    for status_code in sorted(status_map.keys()):
        endpoints = sorted(status_map[status_code], key=lambda x: (x[0], x[1]))
        for uri, ratio in endpoints:
            sig_parts.append(f"{status_code}|{uri}|{round(ratio * 100, 2)}")
    signature = ";".join(sig_parts)

    if not should_send_alert("error_4xx_ratio", alert_key, signature):
        return

    lines: List[str] = []
    lines.append(f"Aplicação: `{app_name}`")
    lines.append(
        f"Limiar: **{ERROR_4XX_RATIO_THRESHOLD * 100:.2f}%** de erros 4xx "
        f"e **{ERROR_4XX_ABS_RATE_THRESHOLD:.4f} req/s** (janela 10m)"
    )
    lines.append("")

    for status_code in sorted(status_map.keys()):
        endpoints = status_map[status_code]

        ep_lines: List[str] = []
        for uri, ratio in sorted(endpoints, key=lambda x: x[1], reverse=True):
            total_5d = req_5d_by_uri.get(uri, 0)
            human_total = format_human_number(total_5d)
            ep_lines.append(f"- `{uri}` ({ratio * 100:.2f}%) — **{human_total} req/5d**")
            #ep_lines.append(f"- `{uri}` ({ratio * 100:.2f}%) — **{total_5d} req/5d**")

        joined = "\n".join(ep_lines)
        lines.append(f"**{status_code}:**\n{joined}")

    description = "\n".join(lines)
    title = "⚠️ Alerta de erros 4xx em múltiplos endpoints"

    send_discord_alert(title, description, color=15105570)


def check_error_5xx_ratio_for_app(app_name: str):
    """
    Alerta de erros 5xx agrupado por app.
    - Calcula rate de erros e total em 10m
    - Faz a razão em Python
    - Usa 2 critérios:
      * razão >= ERROR_5XX_RATIO_THRESHOLD
      * e taxa de erros >= ERROR_5XX_ABS_RATE_THRESHOLD
    Agrupa endpoints por status (500, 502, 503...).

    Melhoria:
    - Adiciona no card o total de requisições do endpoint nos últimos 5 dias (req/5d).
    """

    # (NOVO) total de requests por endpoint nos últimos 5 dias (para enriquecer o card)
    req_5d_by_uri = get_req_count_last_5d_by_uri(app_name)

    errors_promql = (
        'sum by(uri, status) ('
        f'  rate(http_server_requests_seconds_count{{application="{app_name}", status=~"5..", uri!="/actuator/prometheus", uri!="/**"}}[10m])'
        ')'
    )

    total_promql = (
        'sum by(uri) ('
        f'  rate(http_server_requests_seconds_count{{application="{app_name}", uri!="/actuator/prometheus", uri!="/**"}}[10m])'
        ')'
    )

    errors_data = query_prometheus(errors_promql)
    total_data = query_prometheus(total_promql)

    errors_results = errors_data.get("result", [])
    total_results = total_data.get("result", [])

    total_by_uri: Dict[str, float] = {}
    for s in total_results:
        metric = s.get("metric", {})
        uri = metric.get("uri")
        if not uri:
            continue
        v_str = s.get("value", [0, "0"])[1]
        try:
            total_by_uri[uri] = float(v_str)
        except ValueError:
            continue

    status_map: Dict[str, List[Tuple[str, float]]] = {}

    for s in errors_results:
        metric = s.get("metric", {})
        uri = metric.get("uri")
        status = metric.get("status", "5xx")

        if not uri:
            uri = "<desconhecido>"

        err_str = s.get("value", [0, "0"])[1]
        try:
            err_rate = float(err_str)
        except ValueError:
            continue

        total_rate = total_by_uri.get(uri, 0.0)
        denom = total_rate if total_rate > 0 else err_rate
        if denom <= 0:
            continue

        ratio = err_rate / denom

        if ratio >= ERROR_5XX_RATIO_THRESHOLD and err_rate >= ERROR_5XX_ABS_RATE_THRESHOLD:
            status_map.setdefault(status, []).append((uri, ratio))

    if not status_map:
        return

    alert_key = f"{app_name}:5xx_ratio"

    sig_parts: List[str] = []
    for status_code in sorted(status_map.keys()):
        endpoints = sorted(status_map[status_code], key=lambda x: (x[0], x[1]))
        for uri, ratio in endpoints:
            sig_parts.append(f"{status_code}|{uri}|{round(ratio * 100, 2)}")
    signature = ";".join(sig_parts)

    if not should_send_alert("error_5xx_ratio", alert_key, signature):
        return

    lines: List[str] = []
    lines.append(f"Aplicação: `{app_name}`")
    lines.append(
        f"Limiar: **{ERROR_5XX_RATIO_THRESHOLD * 100:.2f}%** de erros 5xx "
        f"e **{ERROR_5XX_ABS_RATE_THRESHOLD:.4f} req/s** (janela 10m)"
    )
    lines.append("")

    for status_code in sorted(status_map.keys()):
        endpoints = status_map[status_code]

        ep_lines: List[str] = []
        for uri, ratio in sorted(endpoints, key=lambda x: x[1], reverse=True):
            total_5d = req_5d_by_uri.get(uri, 0)
            human_total = format_human_number(total_5d)
            ep_lines.append(f"- `{uri}` ({ratio * 100:.2f}%) — **{human_total} req/5d**")
            #ep_lines.append(f"- `{uri}` ({ratio * 100:.2f}%) — **{total_5d} req/5d**")

        joined = "\n".join(ep_lines)
        lines.append(f"**{status_code}:**\n{joined}")

    description = "\n".join(lines)
    title = "🚨 Alerta de erros 5xx em múltiplos endpoints"

    send_discord_alert(title, description, color=15158332)


def check_heap_usage_for_app(app_name: str):
    """
    Painel tiver consumo elevado de memória Heap para uma app.
    Usa used/max em % (agregando por aplicação).
    """
    promql = (
        f'sum(jvm_memory_used_bytes{{application="{app_name}", area="heap"}}) * 100'
        ' / '
        f'sum(jvm_memory_max_bytes{{application="{app_name}", area="heap"}})'
    )

    data = query_prometheus(promql)
    results = data.get("result", [])

    for series in results:
        value_str = series.get("value", [0, "0"])[1]

        try:
            value = float(value_str)  # %
        except ValueError:
            continue

        if value >= HEAP_USAGE_THRESHOLD:
            alert_key = f"{app_name}:heap_usage"
            signature = f"{round(value, 2)}"
            if should_send_alert("heap_usage", alert_key, signature):
                title = "🚨 Consumo elevado de memória Heap"
                description = (
                    f"Aplicação: `{app_name}`\n"
                    f"Uso de heap: **{value:.2f}%**\n"
                    f"Limiar configurado: **{HEAP_USAGE_THRESHOLD:.2f}%**\n"
                    f"Fonte: Prometheus ({PROMETHEUS_URL})"
                )
                send_discord_alert(title, description, color=15158332)


def check_all_apps():
    for app in APP_NAMES:
        logging.info("Checando métricas para aplicação: %s", app)
        check_high_request_rate_for_app(app)
        check_error_4xx_ratio_for_app(app)
        check_error_5xx_ratio_for_app(app)
        check_heap_usage_for_app(app)


# =======================
# Loop principal
# =======================

def main_loop():
    if not DISCORD_WEBHOOK_URL:
        logging.error("A variável de ambiente DISCORD_WEBHOOK_URL é obrigatória.")
    if not APP_NAMES:
        logging.error("Nenhuma aplicação configurada. Use APP_NAMES=app1,app2 ou APP_NAME=app_unica.")
        return

    logging.info("Iniciando serviço de monitoramento Prometheus -> Discord")
    logging.info("Prometheus: %s", PROMETHEUS_URL)
    logging.info("Aplicações monitoradas: %s", ", ".join(APP_NAMES))
    logging.info("Intervalo de checagem: %ss", POLL_INTERVAL_SECONDS)

    while True:
        try:
            check_all_apps()
        except Exception as e:
            logging.exception("Erro inesperado no loop principal: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()

