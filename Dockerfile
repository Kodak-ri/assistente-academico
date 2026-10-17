# Dockerfile para Assistente Acadêmico Autônomo & PWA
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    PORT=8000

# Pacotes de sistema essenciais (curl para healthcheck, tzdata)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cria usuário não-root para segurança
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -s /bin/bash -m appuser

# Instala dependências Python em camada dedicada
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia código-fonte e arquivos estáticos da aplicação
COPY . .

# Cria pasta de dados para o banco SQLite e ajusta permissões
RUN mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
