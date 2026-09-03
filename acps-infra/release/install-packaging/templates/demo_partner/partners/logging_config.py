"""structlog 日志配置：JSON/Console 双模式，由环境变量 LOG_FORMAT 切换。"""

from __future__ import annotations

import logging
import os

import structlog


def configure_logging(log_level: str = "INFO", log_format: str | None = None) -> None:
 """配置 structlog 和 stdlib logging 集成。

    Args:
        log_level: 日志级别，默认 INFO
        log_format: 日志格式，"json" 或 "console"（默认）；可通过 LOG_FORMAT 环境变量覆盖
 """
    fmt = (log_format if log_format is not None else os.getenv("LOG_FORMAT", "console")).lower
    level = getattr(logging, log_level.upper, logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer,
    ]

    if fmt == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer
    else:
        renderer = structlog.dev.ConsoleRenderer

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer,
            renderer,
        ],
    )

    handler = logging.StreamHandler
    handler.setFormatter(formatter)

    root_logger = logging.getLogger
    root_logger.handlers = [handler]
    root_logger.setLevel(level)
