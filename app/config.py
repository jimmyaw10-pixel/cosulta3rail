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

    ibal_retry_on_landing: bool = True
    ibal_recaptcha_retries: int = 3
    ibal_recaptcha_warmup_ms: int = 6000

    # Proxies (opcional). Lista separada por comas: http://user:pass@host:port
    proxy_list: str = ""
    proxy_rotate: bool = True

    recaptcha_site_key: str = "6Le9s5gtAAAAAKi_ut-2vFVRS4m2hqNh8ftm5Omv"
    recaptcha_action: str = "consulta_pago"

    # off | browser | 2captcha | capsolver | auto (browser luego solver en reintentos)
    captcha_solver: str = "off"
    captcha_api_key: str = ""
    captcha_fallback: str = "capsolver"
    captcha_min_score: float = 0.9
    captcha_task_type: str = "ReCaptchaV3M1TaskProxyLess"


settings = Settings()
