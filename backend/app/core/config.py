import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurações centrais da aplicação com suporte a variáveis de ambiente e arquivo .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Parâmetros de Banco de Dados
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'academic_platform.db')}",
    )

    # Parâmetros do Protocolo ntfy
    NTFY_SERVER_URL: str = "https://ntfy.sh"
    NTFY_TOPIC: str = "assistente_academico_dev"
    PUBLIC_API_BASE_URL: str = "http://localhost:8000"

    # Fuso Horário Padrão
    TIMEZONE: str = "America/Sao_Paulo"

    # Credenciais Google / Gemini
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    GOOGLE_CREDENTIALS_FILE: Optional[str] = None
    GOOGLE_TOKEN_FILE: Optional[str] = None


settings = Settings()
