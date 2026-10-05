import base64
import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=getattr(logging, log_level, logging.INFO),
        handlers=[
            # Ротация вместо бесконечного роста: на Render диск у бота
            # эфемерный, но лог мог раздуться до гигабайт за месяц работы.
            RotatingFileHandler(
                "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            ),
            logging.StreamHandler()
        ]
    )
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    
    logger = logging.getLogger(__name__)
    logger.info(f"Логирование настроено на уровень {log_level}")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_ton_address(addr: str) -> str:
    """Приводит TON-адрес к raw-виду 0:<hex>.

    Раньше эта же логика жила дважды: database._normalize_addr и
    marketapp_reports.userfriendly_to_raw. Один источник — меньше шансов,
    что формат адреса начнёт зависеть от того, каким путём он пришёл.
    """
    addr = (addr or "").strip()
    if addr.startswith("0:"):
        return addr.lower()
    if len(addr) == 48 and addr[:2] in ("EQ", "UQ"):
        try:
            urlsafe = addr.replace("-", "+").replace("_", "/")
            padding = (4 - len(urlsafe) % 4) % 4
            urlsafe += "=" * padding
            decoded = base64.b64decode(urlsafe)
            return "0:" + decoded[2:34].hex()
        except Exception:
            pass
    return addr.lower()
