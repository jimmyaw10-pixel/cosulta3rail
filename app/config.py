from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "API Consulta Facturas IBAL"
    app_version: str = "1.0.0"

    # http | browser | auto
    ibal_engine: str = "auto"
    ibal_base_url: str = "https://ibal.gov.co/pagos/"
    ibal_timeout_seconds: float = 360.0
    consulta_timeout_seconds: float = 360.0

    # Vacío = API pública. Si se define, hay que enviar X-API-Key.
    api_key: str = ""
    cors_origins: str = "*"

    ibal_retry_on_landing: bool = True
    ibal_recaptcha_retries: int = 2
    ibal_recaptcha_warmup_ms: int = 3000

    # Proxies DataImpulse Colombia (móvil rotativo)
    proxy_list: str = (
        "http://3bbadbcc00beec1de6fe__cr.co:95c0762ddd2b2a44@gw.dataimpulse.com:823"
    )
    proxy_rotate: bool = True
    ibal_limit_cooldown_seconds: float = 900.0
    ibal_cloudflare_bypass: bool = True

    recaptcha_site_key: str = "6Le9s5gtAAAAAKi_ut-2vFVRS4m2hqNh8ftm5Omv"
    recaptcha_action: str = "consulta_pago"

    captcha_solver: str = "capsolver"
    captcha_api_key: str = (
        "CAP-9C9E0955D64BF5962F13F349B6E31EFB5CF059E21CDBE8C9D507F246E33245B8"
    )
    captcha_fallback: str = "capsolver"
    captcha_min_score: float = 0.9
    captcha_task_type: str = "ReCaptchaV3M1TaskProxyLess"


settings = Settings()
