"""
组件管理模块：各子组件的路由统一导出。
每个子组件独立文件夹，含 router.py 与 service.py，便于不同开发者并行维护。
"""
from . import document_preproc, knowledge_extract, kg_update, nl2cypher, intent_recognition, answer_generation

# 供 main.py 统一注册
COMPONENT_ROUTERS = [
    document_preproc.router,
    knowledge_extract.router,
    kg_update.router,
    nl2cypher.router,
    intent_recognition.router,
    answer_generation.router,
]
