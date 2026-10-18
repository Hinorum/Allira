import os
from dataclasses import dataclass, field


@dataclass
class BotConfig:
    bot_token: str = ""
    openrouter_api_key: str = ""
    default_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    fallback_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    lane_model: str = "google/gemma-4-31b-it:free"
    # Модель для запросов с изображениями: должна принимать image_url.
    vision_model: str = "google/gemma-4-31b-it:free"
    # Модель генерации промпта картинки автопоста: короткий английский
    # текст, отдельная от чата — чтобы не жечь лимиты основной модели.
    image_prompt_model: str = "nvidia/nemotron-3.5-lightning:free"
    news_channel_id: str = ""
    port: int = 10000
    marketapp_api_key: str = ""
    marketapp_wallet: str = ""
    toncenter_api_key: str = ""
    tonapi_api_key: str = ""
    admin_chat_id: str = ""
    admin_user_ids: tuple = ()
    # ЛС-доступ: бот отвечает в личке только создателю. ID — точное
    # совпадение, username — запасной вариант, если ID не указан.
    creator_user_ids: tuple = ()
    creator_username: str = "hinorum"
    bot_username: str = ""
    bot_id: int = 0
    # Крипто-бухгалтерия
    taxpayer_inn: str = ""
    tax_status: str = "resident"
    cost_basis_method: str = "fifo"
    crypto_owner_id: int = 0
    cbr_api_url: str = "https://www.cbr-xml-daily.ru"
    # CEX API (read-only ключи — безопасны для бота).
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    # Токен веб-панели управления (/admin). Пусто — панель отключена.
    admin_panel_token: str = ""

    @classmethod
    def from_env(cls) -> "BotConfig":
        raw_admins = os.getenv("ADMIN_USER_IDS", "").replace(";", ",")
        admin_ids = tuple(
            int(part.strip())
            for part in raw_admins.split(",")
            if part.strip().lstrip("-").isdigit()
        )
        raw_creators = os.getenv("CREATOR_USER_IDS", "").replace(";", ",")
        creator_ids = tuple(
            int(part.strip())
            for part in raw_creators.split(",")
            if part.strip().lstrip("-").isdigit()
        )
        crypto_owner_raw = os.getenv("CRYPTO_OWNER_ID", "").strip()
        crypto_owner_id = (
            int(crypto_owner_raw) if crypto_owner_raw.lstrip("-").isdigit() else 0
        )
        if not crypto_owner_id and creator_ids:
            crypto_owner_id = creator_ids[0]
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            default_model=os.getenv("DEFAULT_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
            fallback_model=os.getenv("FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            lane_model=os.getenv("LANE_MODEL", "google/gemma-4-31b-it:free"),
            vision_model=os.getenv("VISION_MODEL", "google/gemma-4-31b-it:free"),
            image_prompt_model=os.getenv("IMAGE_PROMPT_MODEL", "nvidia/nemotron-3.5-lightning:free"),
            news_channel_id=os.getenv("NEWS_CHANNEL_ID", ""),
            admin_chat_id=os.getenv("ADMIN_CHAT_ID", "").strip(),
            admin_user_ids=admin_ids,
            creator_user_ids=creator_ids,
            creator_username=os.getenv("CREATOR_USERNAME", "hinorum").strip().lstrip("@"),
            port=int(os.getenv("PORT", "10000")),
            marketapp_api_key=os.getenv("MARKETAPP_API_KEY", "").strip(),
            marketapp_wallet=os.getenv("MARKETAPP_WALLET", "").strip(),
            toncenter_api_key=os.getenv("TONCENTER_API_KEY", "").strip(),
            tonapi_api_key=os.getenv("TONAPI_API_KEY", "").strip(),
            taxpayer_inn=os.getenv("TAXPAYER_INN", "").strip(),
            tax_status=os.getenv("TAX_STATUS", "resident").strip().lower(),
            cost_basis_method=os.getenv("COST_BASIS_METHOD", "fifo").strip().lower(),
            crypto_owner_id=crypto_owner_id,
            cbr_api_url=os.getenv("CBR_API_URL", "https://www.cbr-xml-daily.ru").strip().rstrip("/"),
            bybit_api_key=os.getenv("BYBIT_API_KEY", "").strip(),
            bybit_api_secret=os.getenv("BYBIT_API_SECRET", "").strip(),
            okx_api_key=os.getenv("OKX_API_KEY", "").strip(),
            okx_api_secret=os.getenv("OKX_API_SECRET", "").strip(),
            okx_passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
            admin_panel_token=os.getenv("ADMIN_PANEL_TOKEN", "").strip(),
        )
