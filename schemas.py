"""
结构化日志配置 — agent_logger 全局日志记录器

被 graph/、agents/、memory.py 等模块共享，输出控制台 + crawler.log 文件。
"""

import logging
import os


def setup_logger(name: str = "agent_crawler") -> logging.Logger:
    """创建结构化日志记录器，兼容 LangSmith/Langfuse"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # 控制台输出
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # 文件输出：详细日志落盘到项目根目录 crawler.log，便于 GUI 模式下排查
        try:
            log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler.log")
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception:
            # 文件日志不可用时不影响控制台输出
            pass

        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# 全局 logger 实例
agent_logger = setup_logger()
