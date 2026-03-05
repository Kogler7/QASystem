# 组件管理相关路由：知识抽取、知识图谱更新、意图识别等
# main.py 使用: from .routers.component import extract, kg_update; app.include_router(...)

from . import extract  # noqa: F401
from . import kg_update  # noqa: F401
from . import nl2cypher  # noqa: F401
from . import intent_recognition  # noqa: F401
from . import answer_generation  # noqa: F401
from . import document_preproc  # noqa: F401
