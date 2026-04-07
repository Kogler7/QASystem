#!/usr/bin/env python
import os
from pathlib import Path
from dotenv import load_dotenv
from neo4j import GraphDatabase

# 加载环境变量
env_file = Path('/home/admin_user_8/sxm/qasystem/backend/.env')
load_dotenv(env_file)

print(f"环境变量文件: {env_file}")
print(f"文件存在: {env_file.exists()}")
print(f"NEO4J_URI: {os.getenv('NEO4J_URI')}")
print(f"NEO4J_USER: {os.getenv('NEO4J_USER')}")
print(f"NEO4J_PASSWORD: {os.getenv('NEO4J_PASSWORD')}")
print(f"NEO4J_DATABASE: {os.getenv('NEO4J_DATABASE')}")

# 测试连接
uri = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
user = os.getenv('NEO4J_USER', 'neo4j')
password = os.getenv('NEO4J_PASSWORD', 'neo4j2025')
database = os.getenv('NEO4J_DATABASE', 'neo4j')

print(f"\n尝试连接: {uri}")
print(f"用户名: {user}")
print(f"密码: {password[:3]}...")

try:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        result = session.run("RETURN 1 as test")
        print(f"连接成功！结果: {result.single()['test']}")
    driver.close()
except Exception as e:
    print(f"连接失败: {e}")