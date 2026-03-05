import sys
import os
import subprocess
import rdflib

# ================= 1. 文件路径配置 (严格按照要求) =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

OLD_DATA = os.path.join(CURRENT_DIR, "data/old.ttl")
INSERT_DATA = os.path.join(CURRENT_DIR, "data/insert.ttl")
SHAPE_FILE = os.path.join(CURRENT_DIR, "data/shape.ttl")
DELETE_DATA = os.path.join(CURRENT_DIR, "data/delete.ttl") # 占位
REDUCED_TTL = os.path.join(CURRENT_DIR, "results/reduced.ttl")
STRATEGY_LP = os.path.join(CURRENT_DIR, "data/repair_strategy.lp")
RESULT_FILE = os.path.join(CURRENT_DIR, "results/repair_result.txt")
CLINGO_EXE = r"C:/Users/17107/Desktop/merge/clingo-5.4.0-win64/clingo-5.4.0-win64/clingo.exe"

# ================= 2. 环境准备 =================
SRC_PATH = os.path.join(CURRENT_DIR, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

try:
    from upshacl import run_UpSHACL
except ImportError:
    print("❌ 错误")
    sys.exit(1)

def fix_path(path):
    """解决 Windows 盘符导致的 urllib 路径报错"""
    return os.path.relpath(path, start=os.getcwd()).replace("\\", "/")

def run_pipeline():
    # 确保输出目录存在
    os.makedirs(os.path.dirname(REDUCED_TTL), exist_ok=True)

    # ---------------------------------------------------------
    # [阶段 1] 输入: old, insert, delete, shape -> 输出: reduced
    # ---------------------------------------------------------
    print("启动 UpSHACL 生成子图 (Reduced)...")
    try:
        run_UpSHACL(
            fix_path(OLD_DATA), 
            fix_path(SHAPE_FILE), 
            fix_path(INSERT_DATA), 
            fix_path(DELETE_DATA), 
            fix_path(REDUCED_TTL),
            verbose=False
        )
    except Exception as e:
        print(f" 阶段 1 运行失败: {e}")
        return

    # ---------------------------------------------------------
    # [阶段 2] 输入: reduced, shape, strategy -> 输出: result
    # ---------------------------------------------------------
    print(f" [阶段 2/2] 正在基于 Reduced 图谱执行修复推演...")
    
    if not os.path.exists(REDUCED_TTL) or os.path.getsize(REDUCED_TTL) == 0:
        print(" Reduced 图谱为空，意味着未检测到冲突。")
        return

    #  转化为 ASP 事实 (facts.lp)
    asp_facts = []
    g_red = rdflib.Graph().parse(REDUCED_TTL, format="turtle")
    
    for s, p, o in g_red:
        s_name = str(s).split('/')[-1].split('#')[-1]
        p_str = str(p).lower()
        # 谓词规范化，保护 rdf:type
        p_name = "type" if "type" in p_str else str(p).split('/')[-1].split('#')[-1]
        
        # 处理数据类型：数字直接输出，字符串加引号
        o_val = f'"{str(o)}"' if not str(o).isdigit() else str(o)
        
        # 基础事实
        asp_facts.append(f'triple("{s_name}", "{p_name}", {o_val}).')
        
        # 关键逻辑：身份锁定。只有非 type 的属性才会被标记为可修复的冲突。
        # 这是基于你之前的观察：不能因为年龄不对就认为他“不是学生”。
        if p_name != "type":
            asp_facts.append(f'violation("{s_name}", "{p_name}", "Conflict").')

    FACTS_LP = os.path.join(CURRENT_DIR, "results/facts.lp")
    with open(FACTS_LP, "w", encoding="utf-8") as f:
        f.write("\n".join(set(asp_facts)))

    # 运行 Clingo
    # 输入: facts.lp (源自reduced), strategy_lp
    cmd = [CLINGO_EXE, FACTS_LP, STRATEGY_LP, "--opt-mode=optN", "-n", "1", "--quiet=1"]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            f.write(result.stdout)
        
        print("-" * 50)
        print(f"修复建议已生成！\n 结果文件: {RESULT_FILE}")
        print("-" * 50)
    except Exception as e:
        print(f"Clingo 执行出错: {e}")

if __name__ == "__main__":
    run_pipeline()