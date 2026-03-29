import re
import logging
import clingo
import os
from rdflib import Graph, Literal, URIRef
from pyshacl import validate
from pathlib import Path
from neo4j import GraphDatabase

# 尝试导入配置，失败则使用默认值
try:
    from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PWD
except ImportError:
    NEO4J_URI = "bolt://localhost:7687"
    NEO4J_USER = "neo4j"
    NEO4J_PWD = "password"

logger = logging.getLogger(__name__)

class UpSHACLRepairManager:
    def __init__(self):
        self.uri = NEO4J_URI
        self.user = NEO4J_USER
        self.pwd = NEO4J_PWD

    def clean_id(self, text):
        """增强版 ID 清洗：剥离前缀、引号，确保 Clingo 匹配纯净"""
        if not text: return ""
        s = str(text).strip()
        # 处理 URI 格式
        if '/' in s or '#' in s:
            s = s.split('/')[-1].split('#')[-1]
        # 处理常见的 ex: 前缀
        if s.startswith('ex:'):
            s = s[3:]
        # 移除可能干扰 Clingo 的特殊符号
        return s.replace('>', '').replace('<', '').replace('"', '').replace("'", "")

    def preprocess_ttl(self, ttl_content):
        """将 Turtle 转换成 Clingo 逻辑事实 (triple 格式)"""
        if not ttl_content or ttl_content.strip() == "": return ""
        try:
            g = Graph().parse(data=ttl_content, format="turtle")
            facts = []
            for s, p, o in g:
                s_id = self.clean_id(s)
                p_id = self.clean_id(p)
                o_str = str(o).strip()
                
                # 判断是数值还是字符串，数值不加引号
                if isinstance(o, Literal) and re.match(r'^-?\d+(\.\d+)?$', o_str):
                    val = o_str
                else:
                    # 字符串常量在 Clingo 中必须包裹在双引号内
                    val = f'"{self.clean_id(o_str)}"'
                
                facts.append(f'triple("{s_id}","{p_id}",{val}).')
            return "\n".join(facts)
        except Exception as e:
            logger.error(f"🚨 TTL 转换逻辑事实异常: {e}")
            return ""

    def run_logic_stream(self, insert_ttl, delete_ttl, shape_ttl, shape_lp, strategy_logic):
        logger.info("🚀 UpSHACL 核心引擎启动...")
        violated_details = [] 
        
        # --- 1. 数据准备与物理落盘 (用于 DEBUG) ---
        ins_facts = self.preprocess_ttl(insert_ttl)
        
        # 暴力创建结果目录
        results_dir = Path("results")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(results_dir / "facts.lp", "w", encoding="utf-8") as f:
                f.write(ins_facts)
            logger.info(f"✅ 逻辑事实已同步至 {results_dir}/facts.lp")
        except Exception as e:
            logger.error(f"⚠️ 无法写入调试文件: {e}")

        # --- 2. 探照灯调试：在控制台打印输入内容 ---
        print("\n" + "="*50)
        print("🔍 [CLINGO INPUT DEBUG] 事实内容预览:")
        print(ins_facts if ins_facts else "空数据！")
        print("-" * 30)
        print("🔍 [CLINGO INPUT DEBUG] 规则内容预览:")
        print(shape_lp if shape_lp else "空规则！")
        print("="*50 + "\n")

        # --- 3. PySHACL 结构校验 (静态) ---
        if insert_ttl and shape_ttl:
            try:
                data_graph = Graph().parse(data=insert_ttl, format="turtle")
                shacl_graph = Graph().parse(data=shape_ttl, format="turtle")
                conforms, _, results_text = validate(data_graph, shacl_graph=shacl_graph)
                if not conforms:
                    # 提取具体违规的主体和属性
                    nodes = re.findall(r"Focus Node:\s*(.+)", results_text)
                    paths = re.findall(r"Result Path:\s*(.+)", results_text)
                    for n, p in zip(nodes, paths):
                        violated_details.append({
                            "subject": self.clean_id(n),
                            "predicate": self.clean_id(p)
                        })
                    logger.warning(f"❌ SHACL 发现 {len(violated_details)} 处结构违规")
            except Exception as e:
                logger.error(f"🚨 PySHACL 引擎运行异常: {e}")

        # --- 4. Clingo 推演校验 (逻辑) ---
        ctl = clingo.Control(["-Wnone"])
        # 基础传递闭包逻辑
        ctl.add("base", [], "add(S,P,O) :- triple(S,P,O).") 
        # 注入事实
        ctl.add("base", [], ins_facts)
        # 注入外部规则
        if shape_lp and shape_lp.strip():
            ctl.add("base", [], shape_lp)
        
        ctl.ground([("base", [])])

        safe_actions = []
        with ctl.solve(yield_=True) as handle:
            for model in handle:
                # 捕获所有原子，包含 conflict 和 add
                atoms = model.symbols(atoms=True)
                logger.info(f"🧪 Clingo 推演产生的原子总数: {len(atoms)}")
                
                for atom in atoms:
                    # 处理逻辑冲突
                    if atom.name == "conflict":
                        args = atom.arguments
                        if len(args) >= 2:
                            s_id = self.clean_id(str(args[0]))
                            p_id = self.clean_id(str(args[1]))
                            violated_details.append({"subject": s_id, "predicate": p_id})
                    
                    # 处理允许入库的数据
                    elif atom.name == "add":
                        s_id = str(atom.arguments[0]).strip('"')
                        p_id = str(atom.arguments[1]).strip('"')
                        
                        # 冲突拦截逻辑
                        is_blacklisted = any(
                            d["subject"] == s_id and d["predicate"] == p_id 
                            for d in violated_details
                        )
                        
                        if strategy_logic == "STRICT" and is_blacklisted:
                            continue
                        safe_actions.append(str(atom))

        # 结果去重
        unique_violations = [dict(t) for t in {tuple(d.items()) for d in violated_details}]
        logger.info(f"🏁 检测结束。最终冲突数: {len(unique_violations)}, 安全动作数: {len(safe_actions)}")

        return {
            "safe_actions": safe_actions, 
            "violated_details": unique_violations
        }

    def execute_sync_to_db(self, actions):
        """通用物理入库执行器"""
        if not actions: return
        REL_PROPS = ["MANUFACTURED_BY", "HAS_STATUS", "LOCATED_IN", "HAS_TERMDEFINITION"]
        queries = []

        for act in actions:
            match = re.match(r'^add\("(.*?)",\s*"(.*?)",\s*(.*)\)$', str(act).strip())
            if not match: continue
            
            s = match.group(1)
            p = match.group(2)
            o = match.group(3).strip(' "')
            
            if p == "type":
                queries.append(f"MERGE (n {{id: '{s}'}}) SET n:{o}, n:Entity")
            elif p in REL_PROPS:
                queries.append(f"MERGE (a {{id: '{s}'}}) MERGE (b {{id: '{o}'}}) SET a:Entity, b:Entity MERGE (a)-[:`{p}`]->(b)")
            else:
                val = o if o.replace('.', '', 1).isdigit() else f"'{o}'"
                queries.append(f"MERGE (n {{id: '{s}'}}) SET n:Entity, n.`{p}` = {val}")

        if queries:
            try:
                with GraphDatabase.driver(self.uri, auth=(self.user, self.pwd)) as driver:
                    with driver.session() as session:
                        for q in queries:
                            session.run(q)
                logger.info(f"✅ 成功同步 {len(queries)} 条操作至 Neo4j")
            except Exception as e:
                logger.error(f"🚨 Neo4j 写入失败: {e}")

    def process_and_map_data(self, json_data):
        """增强版：将 JSON 转换为 Turtle，并完美提取属性与关系用于前端展示"""
        rdf_lines = ["@prefix ex: <http://example.org/> ."]
        comparison_map = {}
        
        # [新增] 全局 ID 到 名称 的翻译字典
        id_to_name = {}

        graph_data = json_data.get("graph", {})
        
        # 第一遍：收集所有节点属性，并建立名字翻译表
        for node in graph_data.get("nodes", []):
            raw_uid = str(node.get("uid"))
            safe_s = f"n_{raw_uid}"
            comparison_map[safe_s] = {}
            
            # 提取友好的展示名 (优先取 RuleName, 没有就取 name, 最后用 ID)
            props = node.get("properties", {})
            display_name = props.get("RuleName", props.get("name", raw_uid))
            id_to_name[raw_uid] = display_name
            
            label = node.get("label", "Entity")
            rdf_lines.append(f'ex:{safe_s} ex:type ex:{label} .')
            
            # 存入纯文本属性
            for k, v in props.items():
                if not v or str(v) == "无" or k == "confidence": continue
                val_str = str(v)
                if "." in val_str and val_str.replace(".", "", 1).isdigit():
                    val_str = f"str_{val_str}"
                
                comparison_map[safe_s][k] = str(v)
                rdf_lines.append(f'ex:{safe_s} ex:{k} "{val_str}" .')

        # 第二遍：处理关系连线，把关系也当成一种“特殊属性”塞给前端
        for rel in graph_data.get("relationships", []):
            s = f"n_{rel['from_uid']}"
            o = f"n_{rel['to_uid']}"
            p = rel['type'] # 例如 "HAS_CONSTRAINTRULETYPE"
            
            # 获取目标节点的友好名称，如果没名字就显示 ID
            target_raw_uid = str(rel['to_uid'])
            target_display = id_to_name.get(target_raw_uid, f"节点_{target_raw_uid}")

            # 核心修复：把关系目标存进映射表，前端就不会显示"未知"了！
            if s not in comparison_map:
                comparison_map[s] = {}
            # 这里存的是翻译后的名字！
            comparison_map[s][p] = f"指向 -> {target_display}"
            
            rdf_lines.append(f'ex:{s} ex:{p} ex:{o} .')

        input_rdf = "\n".join(rdf_lines)
        return input_rdf, comparison_map