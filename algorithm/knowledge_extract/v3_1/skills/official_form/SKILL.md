---
noteId: "7b0cede0c54b11f192edfbc308186266"
tags: []
name: "official_form"
description: "公文收文处理单、审批单、流转表单；区分字段签名与明确职责。"
version: "1"
stages:
  - "entity"
route_hints:
  - "official_form"

---

本技能只提供领域策略。读取后生成装配计划，引用下面适用于当前阶段的规则段；不输出抽取答案，不改写 System、原文、本体或输出契约。

## rules:entity

### 当前 chunk 是公文表单/收文处理单，必须执行以下特殊约束
- 该 chunk 不是公文正文，不要抽取 DocumentUnit。
- 表单字段标签（收文号、来文字号、来文单位、收文日期、联系电话、拟办意见、领导批示、主送、抄送、主办部门等）不是实体，也不要为纯字段值单独建实体。
- 不要为“已阅/签名”的个人姓名创建 OperationalAgent；审批流转记录中的个人不是主体。
- 但拟办意见/领导批示中明确承担职责的部门、单位、角色（如“技术中心”“规划运营部”“信息公司”“数科公司”）应抽取为 OperationalAgent，并在“职责”属性中归纳其被指派的职责。
- 拟办意见/领导批示中明确的动宾工作事项（如“明确工作承担部门与责任人”“将任务分工纳入部门重点工作”）应抽取为 OperationalActivity；明确的要求/限制抽取为 OperationalConstraint；明确的报送/通知/反馈行为抽取为 OperationalExchange。
- 上述抽取必须逐条对应表单原文并有证据，不得把同一句话拆成大量碎片，也不得臆造未出现的主体。
- 表单提供的公文标题/文号等元数据可作为对应 OfficialDocument 的属性/证据；没有可靠标题 mention 时也可以只输出上述 operational 实体。
