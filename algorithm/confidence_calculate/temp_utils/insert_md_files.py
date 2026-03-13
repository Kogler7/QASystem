import os
import sys
import yaml
from elasticsearch import Elasticsearch, helpers

# 获取脚本所在目录的上级目录（confidence_calculate 根目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

# ===== MD 文件路径 =====
MD_DIRECTORY = "algorithm/confidence_calculate/scripts/"
# ===================================

def load_config(config_path: str = CONFIG_PATH):
    """从 config.yaml 加载 ES 配置"""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return config.get('text', {})

def doc_from_markdown(path: str) -> dict:
    """从 md 文件生成要索引的文档体"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return {"path": path, "content": text}

def index_markdowns(directory: str = MD_DIRECTORY, index_name: str = None):
    """把目录下所有 .md 文件批量索引到 ES"""
    # 检查目录是否存在
    if not os.path.isdir(directory):
        print(f"错误: MD 文件目录不存在: {directory}")
        print("请修改脚本中的 MD_DIRECTORY 变量")
        return
    
    # 加载配置
    text_config = load_config()
    
    # 从配置读取 ES 参数
    es_hosts = text_config.get('es_hosts', ["https://10.126.62.88:9200"])
    es_username = text_config.get('es_username', 'elastic')
    es_password = text_config.get('es_password', '123456')
    es_timeout = text_config.get('es_timeout', 30)
    es_verify_certs = text_config.get('es_verify_certs', False)
    
    # 如果未指定索引名，使用配置中的
    if index_name is None:
        index_name = text_config.get('es_index', 'documents')
    
    print(f"MD 文件目录: {directory}")
    print(f"连接到 Elasticsearch: {es_hosts[0]}")
    print(f"目标索引: {index_name}")
    
    # 连接参数
    es_kwargs = {
        'hosts': es_hosts,
        'request_timeout': es_timeout,
        'retry_on_timeout': True,
        'max_retries': 3,
    }
    
    # 添加认证信息
    if es_username and es_password:
        es_kwargs['basic_auth'] = (es_username, es_password)
    
    # SSL 证书验证
    if 'https' in es_hosts[0] and not es_verify_certs:
        es_kwargs['verify_certs'] = False
        es_kwargs['ssl_show_warn'] = False
    
    # 创建连接
    es = Elasticsearch(**es_kwargs)
    
    # 测试连接
    try:
        if not es.ping():
            print(f"错误: 无法连接到 Elasticsearch {es_hosts[0]}")
            print("请检查:")
            print("  - ES 服务是否已启动")
            print("  - 用户名密码是否正确")
            print("  - 网络是否通畅")
            return
        print("Elasticsearch 连接成功")
        
        # 显示 ES 版本信息
        info = es.info()
        print(f"ES 版本: {info['version']['number']}")
        
    except Exception as e:
        print(f"连接失败: {e}")
        return
    
    # 收集所有 markdown 文件
    actions = []
    for root, _, files in os.walk(directory):
        for fn in files:
            if fn.lower().endswith(".md"):
                full = os.path.join(root, fn)
                rel_path = os.path.relpath(full, directory)
                actions.append({
                    "_index": index_name,
                    "_id": rel_path,
                    "_source": doc_from_markdown(full)
                })
    
    if not actions:
        print(f"在目录 {directory} 中未发现 markdown 文件。")
        return
    
    print(f"找到 {len(actions)} 个 markdown 文件，开始导入...")
    
    # 批量导入
    try:
        success, failed = helpers.bulk(
            es, 
            actions, 
            stats_only=True, 
            raise_on_error=False,
            chunk_size=500
        )
        print(f"已向索引 '{index_name}' 写入 {success} 条文档。")
        if failed > 0:
            print(f"失败: {failed} 条文档。")
    except Exception as e:
        print(f"批量导入失败: {e}")

if __name__ == "__main__":
    index_markdowns()