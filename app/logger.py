from __future__ import annotations

import logging
import logging.config

from app.config import Settings, get_settings


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                }
            },
            "root": {
                "handlers": ["console"],
                "level": settings.log_level.upper(),
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
