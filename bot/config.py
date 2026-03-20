import os

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB: str = os.getenv("POSTGRES_DB", "onsinch_bot")
POSTGRES_USER: str = os.getenv("POSTGRES_USER", "botuser")
POSTGRES_PASSWORD: str = os.environ["POSTGRES_PASSWORD"]

SCRAPE_INTERVAL_MINUTES: int = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "15"))
ONSINCH_BASE_URL: str = os.getenv("ONSINCH_BASE_URL", "https://splendid.onsinch.com")

SESSIONS_DIR: str = "/data/sessions"
LOGIN_TIMEOUT_MS: int = 30_000
API_RETRY_COUNT: int = 3
MESSAGE_SEND_DELAY: float = 0.5

POSITIONS_API_HEADERS: dict = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=utf-8",
    "Origin": ONSINCH_BASE_URL,
    "Referer": f"{ONSINCH_BASE_URL}/react/position?ignoreCapacity=false",
}

POSITIONS_API_BODY: dict = {
    "key": "worker/Positions/Index",
    "meta": {"page": 1, "limit": 100},
    "params": {"attend": True},
}
