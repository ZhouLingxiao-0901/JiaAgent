"""日志配置。

同时输出到控制台和文件（logs/jiaagent.log），文件会自动轮转，
最多占 4 份 x 5MB，不会把硬盘塞满。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.settings import LOG_DIR

LOG_FILE = LOG_DIR / "jiaagent.log"
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        # 重复调用时不要叠加 handler，否则日志会打印好几遍
        root.setLevel(level.upper())
        return

    root.setLevel(level.upper())
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 第三方库的调试日志太多，压到 WARNING
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
