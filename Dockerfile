FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IBAL_ENGINE=browser \
    IBAL_TIMEOUT_SECONDS=360 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PROXY_LIST=http://3bbadbcc00beec1de6fe__cr.co:95c0762ddd2b2a44@gw.dataimpulse.com:823 \
    PROXY_ROTATE=true \
    IBAL_CLOUDFLARE_BYPASS=true \
    CAPTCHA_SOLVER=capsolver \
    CAPTCHA_API_KEY=CAP-9C9E0955D64BF5962F13F349B6E31EFB5CF059E21CDBE8C9D507F246E33245B8 \
    CAPTCHA_MIN_SCORE=0.9 \
    CAPTCHA_TASK_TYPE=ReCaptchaV3M1TaskProxyLess

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 360"]
