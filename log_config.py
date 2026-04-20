import logging
from logging.handlers import RotatingFileHandler

def setup_logging():
    logger = logging.getLogger('my_application_logger')
    logger.setLevel(logging.DEBUG)

    # 检查是否已经添加了 RotatingFileHandler
    if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        handler = RotatingFileHandler('my_app.log', maxBytes=1024*1024*5, backupCount=5)
        # 毫秒级时间戳 + 等级左对齐 + 消息对齐，方便 grep 和快速扫读
        formatter = logging.Formatter(
            fmt='%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
        handler.setFormatter(formatter)

        logger.addHandler(handler)

    return logger

