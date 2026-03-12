import mysql.connector
from mysql.connector import Error
from typing import List, Dict, Any, Optional
import logging


class MySQLClient:
    def __init__(self, host: str, port: int, database: str,
                 username: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.connection = None

    def connect(self) -> bool:
        """连接到MySQL数据库"""
        try:
            self.connection = mysql.connector.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.username,
                password=self.password,
                charset='utf8mb4'
            )
            print("MySQL连接成功")
            return True
        except Error as e:
            print(f"MySQL连接失败: {e}")
            return False

    def create_table(self, create_sql: str) -> bool:
        """创建表"""
        try:
            cursor = self.connection.cursor()
            cursor.execute(create_sql)
            self.connection.commit()
            cursor.close()
            return True
        except Error as e:
            print(f"创建表失败: {e}")
            return False

    def insert_excel_data(self, table_name: str, columns: List[str],
                          data: List[List]) -> int:
        """
        插入Excel数据到MySQL
        返回插入的行数
        """
        if not data:
            return 0

        try:
            cursor = self.connection.cursor()

            # 准备插入语句
            placeholders = ', '.join(['%s'] * len(columns))
            column_names = ', '.join([f"`{col}`" for col in columns])
            insert_sql = f"INSERT INTO `{table_name}` ({column_names}) VALUES ({placeholders})"

            # 批量插入
            cursor.executemany(insert_sql, data)
            self.connection.commit()

            row_count = cursor.rowcount
            cursor.close()

            print(f"成功插入 {row_count} 行数据到表 {table_name}")
            return row_count

        except Error as e:
            print(f"插入数据失败: {e}")
            return 0

    def create_document_metadata_table(self):
        """创建文档元数据表"""
        create_sql = """
        CREATE TABLE IF NOT EXISTS document_metadata (
            id INT AUTO_INCREMENT PRIMARY KEY,
            doc_id VARCHAR(64) UNIQUE,
            file_name VARCHAR(255),
            file_type VARCHAR(20),
            file_path VARCHAR(500),
            file_size BIGINT,
            is_scanned BOOLEAN DEFAULT FALSE,
            processed_at DATETIME,
            es_indexed BOOLEAN DEFAULT FALSE,
            mysql_stored BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_file_type (file_type),
            INDEX idx_processed_at (processed_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

        return self.create_table(create_sql)

    def close(self):
        """关闭连接"""
        if self.connection and self.connection.is_connected():
            self.connection.close()