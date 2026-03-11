import re
import logging
import clingo
from rdflib import Graph, Literal
from pyshacl import validate
from src.store import neo4j_db
from src.config import FACTS_LP
from pathlib import Path

logger = logging.getLogger(__name__)

class UpSHACLRepairManager:
    def __init__(self):
        self.db = neo4j_db

    # 1. 预处理：无敌的格式清洗
    def preprocess_ttl(self, ttl_content, action_type="add"):
        if not ttl_content or ttl_content.strip() == "": return ""
        try:
            g = Graph().parse(data=ttl_content, format="turtle")
            facts = []
            for s, p, o in g:
                s_id = re.split(r'[/#:]', str(s))[-1]
                p_id = re.split(r'[/#:]', str(p))[-1]
                
                # 🎯 核心修复：强力数字识别，防止 Clingo 字符串与数字对比错乱
                o_str = str(o).strip()
                if isinstance(o, Literal) and re.match(r'^-?\d+(\.\d+)?$', o_str):
                    val = o_str
                else:
                    o_id = re.split(r'[/#:]', o_str)[-1]
                    val = f'"{o_id}"'
                
                facts.append(f'triple("{s_id}","{p_id}",{val}).')
            return "\n".join(facts)
        except Exception as e:
            logger.error(f"❌ TTL 预处理异常: {e}")
            return ""

    # 2. 核心推演
    # 2. 核心推演
    # ============================================================
    # 替换这里：核心推演层
    # ============================================================
    def run_logic_stream(self, insert_ttl, delete_ttl, shape_ttl, shape_lp, strategy_logic):
        logger.info("🚄 UpSHACL 双引擎流水线启动...")
        violated_subjects = set()
        
        if insert_ttl:
            try:
                data_graph = Graph().parse(data=insert_ttl, format="turtle")
                shacl_graph = Graph().parse(data=shape_ttl, format="turtle")
                conforms, _, results_text = validate(data_graph, shacl_graph=shacl_graph)

                if not conforms:
                    with open(FACTS_LP, "w", encoding="utf-8") as f: f.write(results_text)
                    nodes = re.findall(r"Focus Node:\s*(.+)", results_text)
                    violated_subjects = {re.split(r'[/#:]', str(n))[-1] for n in nodes}
                    logger.warning(f"🚫 违规主体黑名单: {violated_subjects}")
            except Exception as e:
                logger.error(f"🚨 PySHACL 崩溃: {e}")

        ins_facts = self.preprocess_ttl(insert_ttl, "add")
        del_facts = self.preprocess_ttl(delete_ttl, "del")
        
        # 加上 "-Wnone" 参数，意思是关闭所有无关紧要的 Warning 和 Info
        ctl = clingo.Control(["-Wnone"])
        ctl.add("base", [], shape_lp)   # 🎯 喂入：规程逻辑 (shape.lp)
        
        # 🎯 核心破案修复：如果你传入了策略代码(repair_strategy.lp的内容)，也喂给 Clingo！
        is_strict_mode = (strategy_logic == "STRICT")
        if strategy_logic and not is_strict_mode:
            ctl.add("base", [], strategy_logic) 
            
        ctl.add("base", [], ins_facts) 
        ctl.add("base", [], del_facts) 
        ctl.ground([("base", [])])

        safe_actions = []
        with ctl.solve(yield_=True) as handle:
            for model in handle:
                for atom in model.symbols(shown=True):
                    if atom.name in ["add", "del"]:
                        s_id = str(atom.arguments[0]).strip('"')
                        # 只有在明确传入 STRICT 时才启用 Python 级拦截
                        if is_strict_mode and s_id in violated_subjects:
                            continue
                        safe_actions.append(str(atom))

        if safe_actions:
            self.execute_sync_to_db(safe_actions)
            print(f"✅ 成功：同步了 {len(safe_actions)} 条动作。")
        else:
            print("⚠️ 警告：策略引擎没有产出任何动作 (0 个动作)！")
        
        return safe_actions

    # ============================================================
    # 替换这里：路径读取层
    # ============================================================
    def run_logic_files(self, insert_path, delete_path, shape_ttl_path, shape_lp_path, strategy_path):
        read = lambda p: Path(p).read_text(encoding='utf-8') if p and Path(p).exists() else ""
        
        # 🎯 核心破案修复：如果 strategy 传的是个 .lp 路径，就去把它读成代码！
        strategy_content = read(strategy_path) if strategy_path.endswith('.lp') else strategy_path
        
        return self.run_logic_stream(
            read(insert_path), 
            read(delete_path), 
            read(shape_ttl_path), 
            read(shape_lp_path), 
            read(strategy_path)  # 👈 现在这里传进来的才是真正的策略代码！
        )

    # 3. 物理入库：增强版正则解析
    def execute_sync_to_db(self, actions):
        rel_preds = ["MANUFACTURED_BY", "HAS_STATUS", "PART_OF", "TRAIN_LINE"]
        for action in actions:
            try:
                act_type = "add" if action.startswith("add") else "del"
                # 🎯 核心修复：更宽容的正则，无视参数之间的空格
                match = re.search(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(.*)\)', action)
                if not match: continue
                s, p, o_raw = match.groups()
                o = o_raw.strip().rstrip(')').strip('"')

                if act_type == "add":
                    if p == "type":
                        query = f"MERGE (n {{id: '{s}'}}) SET n:{o}"
                    elif p in rel_preds:
                        query = f"MERGE (a {{id: '{s}'}}) MERGE (b {{id: '{o}'}}) MERGE (a)-[:{p}]->(b)"
                    else:
                        val = o if o.isdigit() else f"'{o}'"
                        query = f"MERGE (n {{id: '{s}'}}) SET n.{p} = {val}"
                else:
                    if p in rel_preds:
                        query = f"MATCH (a {{id: '{s}'}})-[r:{p}]->(b {{id: '{o}'}}) DELETE r"
                    else:
                        query = f"MATCH (n {{id: '{s}'}}) REMOVE n.{p}"

                self.db.query(query)
            except Exception as e:
                logger.error(f"❌ 同步失败: {e} | Action: {action}")