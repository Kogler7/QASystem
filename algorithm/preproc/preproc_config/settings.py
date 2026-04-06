import os
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Settings:
    # 文件处理设置
    SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.html', '.htm'}
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB

    # 临时文件目录
    TEMP_DIR = Path("temp_files")
    PROCESSED_DIR = Path("processed_files")

    # OCR设置
    OCR_LANGUAGES = ['ch', 'en']
    OCR_USE_GPU = False

    # 数据库设置
    ES_INDEX_NAME = "documents"
    ES_HOST = "10.126.62.88"
    ES_PORT = 9200

    MYSQL_HOST = "localhost"
    MYSQL_PORT = 3306
    MYSQL_DATABASE = "rail_assistant"

    @classmethod
    def init_dirs(cls):
        """初始化目录"""
        cls.TEMP_DIR.mkdir(exist_ok=True)
        cls.PROCESSED_DIR.mkdir(exist_ok=True)
