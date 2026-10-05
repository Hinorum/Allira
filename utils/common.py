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
