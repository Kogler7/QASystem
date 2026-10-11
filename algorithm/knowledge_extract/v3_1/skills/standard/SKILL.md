---
noteId: "7b0d14f1c54b11f192edfbc308186266"
tags: []
name: "standard"
description: "标准、规范；识别标准身份、章条结构、机构角色与技术要求。"
version: "1"
stages:
  - "entity"
  - "property"
  - "relation"
  - "repair"
route_hints:
  - "standard"

---

本技能只提供领域策略。读取后生成装配计划，引用下面适用于当前阶段的规则段；不输出抽取答案，不改写 System、原文、本体或输出契约。

## rules:all

STANDARD_PROMPT_EXTENSION v2
领域表达提示：若文本是标准/规范，重点识别标准号、标准标题、章条编号、要求/试验/检验条款、引用标准、约束指标和适用对象。
这些提示只帮助映射 ontology 已声明字段；不得新增实体类型、关系、字段或无证据事实。

标准抽取精度规则：
- 机构角色优先使用最具体的类型：发布机构 Publishing Organization、提出机构 Proposing Organization、归口部门 CompetentDepartment、起草单位 Drafting Organization、组织实施单位 Implementing Organization 等；不要额外创建父类 Organization 来表达同一个机构。
- 一个标准只保留一个 Standard 实体，不要将标准编号、标题或简称拆成多个 Standard。
- 标准整体对象使用 StandardizationObject（例如“电动汽车用动力蓄电池单体”）；不要在条款中为同一对象的简称/别名另建 Subject of Standardization。
- 若文本中的“电池单体”“锂离子电池单体”“金属氢化物镍电池单体”等只是同一标准化对象在不同条款中的表达，应复用已有 StandardizationObject，不要创建冗余对象实体。
- Clause、Subclause、Section、Annex 等结构实体只按实际标题层级创建，不要同时用多个结构类型重复表达同一个标题。
- 不要因为条款中出现了多个约束数值，就把同一条要求拆成多个互相独立的 Constrain；先合并为同一特性的约束，再在属性中记录最大值/最小值/单位。

