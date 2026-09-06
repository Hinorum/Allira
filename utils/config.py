import os
from dataclasses import dataclass, field


@dataclass
class BotConfig:
    bot_token: str = ""
    openrouter_api_key: str = ""
    default_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    fallback_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    lane_model: str = "google/gemma-4-31b-it:free"
    news_channel_id: str = ""
    port: int = 10000
    marketapp_api_key: str = ""
    marketapp_wallet: str = ""
    bot_username: str = ""
    bot_id: int = 0

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            default_model=os.getenv("DEFAULT_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
            fallback_model=os.getenv("FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            lane_model=os.getenv("LANE_MODEL", "google/gemma-4-31b-it:free"),
            news_channel_id=os.getenv("NEWS_CHANNEL_ID", ""),
            port=int(os.getenv("PORT", "10000")),
            marketapp_api_key=os.getenv("MARKETAPP_API_KEY", ""),
            marketapp_wallet=os.getenv("MARKETAPP_WALLET", ""),
        )
