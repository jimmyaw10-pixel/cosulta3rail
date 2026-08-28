from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "API Consulta Facturas IBAL"
    app_version: str = "1.0.0"

    # http | browser | auto
    ibal_engine: str = "auto"
    ibal_base_url: str = "https://ibal.gov.co/pagos/"
    ibal_timeout_seconds: float = 180.0
    consulta_timeout_seconds: float = 180.0

    # Vacío = API pública. Si se define, hay que enviar X-API-Key.
    api_key: str = ""
    cors_origins: str = "*"

    cache_ttl_seconds: int = 300

    recaptcha_site_key: str = "6Le9s5gtAAAAAKi_ut-2vFVRS4m2hqNh8ftm5Omv"


settings = Settings()
