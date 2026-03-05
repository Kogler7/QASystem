import logging
import os
from pathlib import Path
from datetime import datetime

def setup_logging(log_dir: str = "logs", log_level: str = "INFO"):
    """
    设置日志配置
    
    Args:
        log_dir: 日志目录
        log_level: 日志级别
    """
    # 创建日志目录
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    
    # 生成日志文件名
    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_path / f"kg_update_{current_time}.log"
    
    # 日志格式
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # 配置根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # 清除已有的处理器
    root_logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_formatter = logging.Formatter(log_format, datefmt=date_format)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, log_level.upper()))
    file_formatter = logging.Formatter(log_format, datefmt=date_format)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    
    # 记录日志系统启动
    logger = logging.getLogger(__name__)
    logger.info(f"日志系统已启动，级别: {log_level}")
    logger.info(f"日志文件: {log_file}")
    
    return str(log_file)