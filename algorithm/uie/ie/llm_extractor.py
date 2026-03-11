# llm_extractor.py

import logging
import time
from ie.utils import BaseLLM, ApiLLM, simple_post_process


logger = logging.getLogger(__name__)


class LLMExtractor:
    """
    统一的本体驱动抽取入口：
    - 输入：UML、主对象名称、待抽取文本
    - 输出：提取后的 JSON（或解析失败时的原始字符串）

    可选 backend：
    - 默认：ApiLLM（连接部署的 vLLM 服务）
    - 显式传入其他 BaseLLM 实现
    """

    def __init__(self, backend: BaseLLM | None = None):
        self.backend = backend if backend is not None else ApiLLM()

    @staticmethod
    def build_uml_system_prompt(uml: str, main_object: str) -> str:
        return f"""你是一个专家级的本体驱动信息抽取引擎。
你的任务是根据提供的【领域本体规范】，从【待抽取文本】中提取结构化知识。

### 1. 领域本体规范 (PUML/SysML)
@startuml
{uml}
@endum

### 2. 实例化准则
- **类实例化 (Class Mapping)**：每一个 JSON 对象必须对应 UML 中的一个类。
- **属性映射 (Property Mapping)**：
    - 基本属性：严格按照类定义的字段名输出，确保数据类型一致（如 String, List, Double）。
    - 缺失处理：若文本中未提及某必填属性，且无法根据上下文推断，统一填充 null。
- **层级嵌套逻辑 (Nesting & Aggregation)**：
    - 组合关系 (Aggregation/Composition)：若 UML 中定义了 A 包含 B，则在 JSON 中 A 的字段下嵌套 B 的完整对象。
    - 递归处理：若存在递归定义（如 Composite 包含 Sub-elements），必须完整展开层级，严禁截断逻辑。
- **多态处理 (Polymorphism)**：若某个字段的类型是一个基类，在输出时必须以其实际的子类名作为键名，以体现具体的逻辑类型。

### 主对象与输出格式
- **主对象**：本规范的主对象为 UML 中的 **{main_object}** 类（根类）。
- **输出结构**：顶层为 JSON 对象 `{{\"{main_object}\": [{{...}}, ...]}}`，键为主对象类名，值为该主对象实例的数组。
- **字段归属**：元素内包含主对象的直接属性及组合关系下的子对象，其他均为该主对象的属性或子对象，无需额外包装键。

### 3. 抽取质量要求
- **原文忠实性**：所有提取的属性值必须来源于原文或对原文逻辑的直接映射，严禁引入本体定义之外的外部知识。
- **术语规范**：识别并提取文本中的核心行业术语，填充至相应字段中。
- **唯一性标识**：为每个实例化的顶级对象分配一个在当前上下文中唯一的 ID（若本体有定义 ID 字段）。

### 4. 输出约束
- 仅输出一个纯净的 JSON 对象，形如 `{{\"{main_object}\": [...]}}`。
- 不含任何解释性文字、Markdown 标签或推理过程。
"""

    def extract(self, uml: str, main_object: str, text: str):
        """
        一步抽取接口：
        - 输入：uml, main_object, text
        - 输出：JSON（或字符串）
        """
        system_prompt = self.build_uml_system_prompt(uml, main_object)
        logger.info(
            "LLM extract start: main_object=%s text_len=%d prompt_len=%d",
            main_object, len(text), len(system_prompt),
        )
        t0 = time.perf_counter()
        raw = self.backend.run(system_prompt, text)
        elapsed = time.perf_counter() - t0
        parsed = simple_post_process(raw)
        is_json = isinstance(parsed, (list, dict))
        logger.info(
            "LLM extract done: main_object=%s elapsed=%.2fs raw_len=%d parsed_json=%s",
            main_object, elapsed, len(raw) if isinstance(raw, str) else 0, is_json,
        )
        return parsed

if __name__ == "__main__":
    extractor = LLMExtractor()
    text = """
序号	日期	故障状况	所属系统	  故障原因	处理方法	故障详细分类	故障地点	是否晚点	车次	故障值	晚点次数	故障次数	月度	季度	年度
1	1月2日	7:20 109110在白云公园上行对标-40cn，屏蔽门没有联动	ATP/ATO			对标不准	白云公园		8A109110				1	1	2014
2	1月2日	12:30 1209次（2A035036）在嘉禾望岗下行司机开钥匙后信号屏出现黑屏、ATO灯不亮、车门自动关闭，行调组织司机复位ATP以RM模式动车后恢复正常	ATP/ATO	ATP的B通道5V电源板块工作不稳定，更换后恢复正常。		电源板	嘉禾望岗		2A035036				1	1	2014
3	1月2日	12：40分，广州南站TC572区段红光带后变粉红光带，逻空后恢复正常	SICAS			轨道电路	大洲出厂线						1	1	2014
4	1月2日	广州南站巡检时RTU有报警信息	ATS		重启RTU后恢复正常	RTU	广州南站						1	1	2014
5	1月3日	13:53 MMI显示大洲出厂线TC572轨道区段红光带,瞬间变为粉红光带,行调通知车站逻空后恢复正常	SICAS	接收端轨旁盒内较为潮湿	更换接收端调谐单元、转换单元	轨道电路	大洲出厂线						1	1	2014
6	1月3日	6:07 列车在江泰路联锁区运营停车点不能自动取消。OCC将故障报通号调度。6:23 经信号人员处理后恢复正常。	ATS			运营停车点不能自动取消	江泰路						1	1	2014
7	1月4日	10:39 2304次（ 8A119120）以ATO模式在白云公园上行对标+50cm，车门正常打开，屏蔽门没有联动打开，司机手动开关屏蔽门后恢复正常。 12:27 2306次（8A119120）以ATO模式在白云公园上行对标-40cm，车门正常打开，屏蔽门没有联动打开	ATP/ATO		更换了同步环线检测板	对标不准	白云公园		8A119120				1	1	2014
8	1月4日	13:44 2009次（8A107108）以ATO模式在江夏下行对标-40cm，列车没有收到开门使能信号，司机按压4S04（强行开门按钮）给出开门使能信号。后续的0211次（8A101102）、3107次（8A119120）在该处出现同样故障。	ATP/ATO		更换了同步环线报警板	对标不准	江夏		8A107108				1	1	2014
9	1月5日	9：45分，江泰路站2趟车上行屏蔽门不能联动关闭。	ATP/ATO	多路接收器CPU850工作不稳定导致。		屏蔽门不能联动	江泰路						1	1	2014
10	1月5日	15：41，145146车在市二宫上行对标-20cm,屏蔽门不能联动打开。	ATP/ATO	多路接收器CPU850工作不稳定导致。		屏蔽门不能联动	市二宫						1	1	2014
"""

    # extractor.extract("RuleType", text)
    extractor.extract("Term", text)
    extractor.extract("SystemElement", text)
