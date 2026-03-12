import re
import logging
import clingo
from rdflib import Graph, Literal, URIRef
from pyshacl import validate
from pathlib import Path
from neo4j import GraphDatabase
from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PWD, FACTS_LP

# 保底配置
try:
    from src.config import FACTS_LP
except:
    FACTS_LP = "facts.lp"

logger = logging.getLogger(__name__)

class UpSHACLRepairManager:
    def __init__(self):
        # 云数据库凭据
        self.uri = NEO4J_URI
        self.user = NEO4J_USER
        self.pwd = NEO4J_PWD

    def clean_id(self, text):
        if not text: return ""
        s = str(text).strip()
        
        # 🎯 只有在处理 URI 时才切分，保留正常的括号和中文
        if '/' in s or '#' in s:
            s = s.split('/')[-1].split('#')[-1]
        
        # 🎯 撕掉 ex: 前缀，但绝对不准动括号
        if s.startswith('ex:'):
            s = s[3:]
            
        # 只清理尖括号和多余的引号，保留 ()
        return s.replace('>', '').replace('<', '').replace('"', '').replace("'", "")

    def preprocess_ttl(self, ttl_content):
        if not ttl_content or ttl_content.strip() == "": return ""
        try:
            g = Graph().parse(data=ttl_content, format="turtle")
            facts = []
            for s, p, o in g:
                s_id = self.clean_id(s)
                p_id = self.clean_id(p)
                o_str = str(o).strip()
                if isinstance(o, Literal) and re.match(r'^-?\d+(\.\d+)?$', o_str):
                    val = o_str
                else:
                    val = f'"{self.clean_id(o_str)}"'
                facts.append(f'triple("{s_id}","{p_id}",{val}).')
            return "\n".join(facts)
        except Exception as e:
            logger.error(f"❌ TTL 预处理异常: {e}")
            return ""

    def run_logic_stream(self, insert_ttl, delete_ttl, shape_ttl, shape_lp, strategy_logic):
        logger.info("🚄 UpSHACL 双引擎精准推演启动...")
        violated_details = [] # 🎯 核心：记录 (主体, 属性) 违规对
        
        if insert_ttl:
            try:
                data_graph = Graph().parse(data=insert_ttl, format="turtle")
                shacl_graph = Graph().parse(data=shape_ttl, format="turtle")
                conforms, _, results_text = validate(data_graph, shacl_graph=shacl_graph)

                if not conforms:
                    # 🎯 精准提取：谁的哪个属性违规了
                    nodes = re.findall(r"Focus Node:\s*(.+)", results_text)
                    paths = re.findall(r"Result Path:\s*(.+)", results_text)
                    for n, p in zip(nodes, paths):
                        violated_details.append({
                            "subject": self.clean_id(n),
                            "predicate": self.clean_id(p)
                        })
                    logger.warning(f"🚫 精准违规名单: {violated_details}")
            except Exception as e:
                logger.error(f"🚨 PySHACL 崩溃: {e}")

        ins_facts = self.preprocess_ttl(insert_ttl)
        ctl = clingo.Control(["-Wnone"])
        ctl.add("base", [], "add(S,P,O) :- triple(S,P,O).") # 默认全部 add
        ctl.add("base", [], ins_facts)
        ctl.ground([("base", [])])

        safe_actions = []
        with ctl.solve(yield_=True) as handle:
            for model in handle:
                for atom in model.symbols(shown=True):
                    if atom.name == "add":
                        s_id = str(atom.arguments[0]).strip('"')
                        p_id = str(atom.arguments[1]).strip('"')
                        
                        # 🎯 去连带责任逻辑：
                        # 只有当这个 (s_id, p_id) 组合在违规名单里时，才拦截。
                        # 同一主体的其他属性直接放行！
                        is_blacklisted = any(d["subject"] == s_id and d["predicate"] == p_id for d in violated_details)
                        
                        if strategy_logic == "STRICT" and is_blacklisted:
                            continue
                        safe_actions.append(str(atom))

        return {
            "safe_actions": safe_actions, 
            "violated_details": violated_details
        }

    # --- algorithm/conflict_detection/src/core.py ---

    # --- algorithm/conflict_detection/src/core.py ---

    def execute_sync_to_db(self, actions):
        """极简版物理入库引擎"""
        if not actions: 
            return
            
        REL_PROPS = ["MANUFACTURED_BY", "HAS_STATUS", "LOCATED_IN"]
        queries = []

        for act in actions:
            # 🎯 极简正则：直接按逗号切成三块，保留一切括号和中文
            match = re.match(r'^add\((.*?),\s*(.*?),\s*(.*)\)$', str(act).strip())
            if not match:
                continue
            
            # 🎯 掐头去尾，把两边的引号和空格扒掉
            s = self.clean_id(match.group(1).strip(' "\''))
            p = self.clean_id(match.group(2).strip(' "\''))
            o = match.group(3).strip(' "\'')
            
            # 🎯 极简 Cypher 生成
            if p == "type":
                queries.append(f"MERGE (n {{id: '{s}'}}) SET n:{o}, n:Entity")
            elif p in REL_PROPS:
                queries.append(f"MERGE (a {{id: '{s}'}}) MERGE (b {{id: '{o}'}}) SET a:Entity, b:Entity MERGE (a)-[:`{p}`]->(b)")
            else:
                val = o if o.replace('.', '', 1).isdigit() else f"'{o}'"
                queries.append(f"MERGE (n {{id: '{s}'}}) SET n:Entity, n.`{p}` = {val}")

        # 🎯 极简入库执行 (自动管理连接)
        if queries:
            try:
                with GraphDatabase.driver(self.uri, auth=(self.user, self.pwd)) as driver:
                    with driver.session() as session:
                        for q in queries:
                            logger.info(f"🚀 Cypher: {q}")
                            session.run(q)
                logger.info(f"✅ 成功物理同步 {len(queries)} 条数据")
            except Exception as e:
                logger.error(f"❌ 数据库写入失败: {e}")