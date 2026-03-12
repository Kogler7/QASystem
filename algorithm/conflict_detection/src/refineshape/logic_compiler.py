import os
from pathlib import Path
from src.config import config  # 🎯 确保导入的是新的 config 对象

class LogicCompiler:
    def __init__(self, output_dir=None):
        # 优先使用传入的目录，否则使用 config 里的 data 目录
        self.output_dir = Path(output_dir) if output_dir else config.DATA_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def compile(self, logic_data):
        """
        将 LLM 返回的逻辑数据编译为 TTL 和 LP 文件
        """
        if not logic_data:
            print("⚠️ 编译器收到空数据，跳过生成。")
            return

        print(f"📝 正在编译逻辑至: {self.output_dir}")

        # 1. 生成 SHACL (.ttl) - 直接使用 config 锁定的路径
        self._generate_ttl(logic_data, config.SHAPE_TTL)
        
        # 2. 生成 Clingo (.lp) - 直接使用 config 锁定的路径
        self._generate_lp(logic_data, config.SHAPE_LP)
        
        print("✅ 逻辑文件编译成功！")

    def _generate_ttl(self, data, file_path):
        # 这里的逻辑根据你的业务需求生成 SHACL 文本
        # 示例：
        ttl_content = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .

ex:TrainShape a sh:NodeShape ;
    sh:targetClass ex:Train ;
    sh:property [
        sh:path ex:speed ;
        sh:minInclusive {data.get('min_speed', 0)} ;
    ] .
"""
        file_path.write_text(ttl_content, encoding='utf-8')

    def _generate_lp(self, data, file_path):
        # 生成 Clingo 逻辑
        # 示例：
        lp_content = f"""
% 自动生成的规程逻辑
violation(S) :- triple(S, "speed", V), V < {data.get('min_speed', 0)}.
"""
        file_path.write_text(lp_content, encoding='utf-8')