import os
from dataclasses import dataclass, field

# NFT-коллекции TON для автопостинга: имя → адрес.
# Lost Dogs — коллекция бота (см. промпты).
DEFAULT_COLLECTIONS: dict = {
    "Lost Dogs": "EQAl_hUCAeEv-fKtGxYtITAS6PPxuMRaQwHj0QAHeWe6ZSD0",
}


def _parse_collections(raw: str) -> dict:
    """Разбор NFT_COLLECTIONS: `Имя:адрес,Имя2:адрес2` или просто адреса.

    Пустая строка — дефолтный watchlist (Lost Dogs). Имя без адреса
    игнорируется: без адреса коллекцию не опросить.
    """
    raw = (raw or "").strip()
    if not raw:
        return dict(DEFAULT_COLLECTIONS)

    result: dict = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # «0:abc…» — raw-адрес TON, двоеточие внутри, а не разделитель.
        if chunk.startswith("0:"):
            result[f"Коллекция {len(result) + 1}"] = chunk
            continue
        if ":" in chunk:
            name, _, address = chunk.partition(":")
            name = name.strip()
            address = address.strip()
            if name and address:
                result[name] = address
            continue
        # Голый адрес — имя возьмём из дефолтного списка, если он там есть
        for known_name, known_address in DEFAULT_COLLECTIONS.items():
            if known_address.lower() == chunk.lower():
                result[known_name] = known_address
                break
        else:
            result[f"Коллекция {len(result) + 1}"] = chunk
    return result or dict(DEFAULT_COLLECTIONS)


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
    # NFT-коллекции для автопостинга: имя → адрес
    nft_collections: dict = field(default_factory=lambda: dict(DEFAULT_COLLECTIONS))
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
    # Etherscan V2: один ключ на много сетей. Пусто — EVM-кошельки не
    # сканируются. EVM_CHAIN_IDS — через запятую: 1 (Ethereum), 56 (BSC),
    # 137 (Polygon), 8453 (Base), 42161 (Arbitrum), 10 (Optimism).
    etherscan_api_key: str = ""
    evm_chain_ids: str = ""
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
            nft_collections=_parse_collections(os.getenv("NFT_COLLECTIONS", "")),
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
            etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip(),
            evm_chain_ids=os.getenv("EVM_CHAIN_IDS", "").strip(),
            admin_panel_token=os.getenv("ADMIN_PANEL_TOKEN", "").strip(),
        )
