FROM python:3.9-slim
WORKDIR /app
# Copiar requirements.txt
COPY requirements.txt .
# Instalar dependências
RUN pip install --no-cache-dir -r requirements.txt
# Copiar o script principal
COPY app.py .
# Alterar URL conforme seu prometheus esteja respondendo
ENV PROMETHEUS_URL=https://meu_prometheus.com.br \
    PROMETHEUS_VERIFY_SSL=true \
    POLL_INTERVAL_SECONDS=300 \
    ALERT_COOLDOWN_SECONDS=300
CMD ["python", "app.py"]

