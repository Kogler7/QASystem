from algorithm.ans.QA.prompt_list import *
from algorithm.ans.QA.utils import *

def clean_relations_neo4j(string, entity_id, available_relations, direction="outgoing"):
    pattern = r'\{([^（]+)（评分：([0-9.]+)）\}'
    import re

    relations = []
    matches = re.findall(pattern, string)

    if not matches:
        # 尝试其他格式：{关系名称 (评分: 分数)}
        pattern2 = r'\{\s*([^（(]+)\s*[（(]\s*评分\s*[:：]\s*([0-9.]+)\s*[）)]\s*\}'
        matches = re.findall(pattern2, string)
    for match in matches:
        if len(match) >= 2:
            relation = match[0].strip()
            score_str = match[1].strip()

            if relation in available_relations:
                try:
                    score = float(score_str)
                    # 确保分数在合理范围内
                    if score < 0.01:  # 太小的分数
                        score = 0.01
                    elif score > 1.0:  # 超过1的分数
                        score = 1.0

                    relations.append({
                        "entity": entity_id,
                        "relation": relation,
                        "score": score,
                        "head": (direction == "outgoing")
                    })
                except ValueError:
                    pass

    if relations:
        relations.sort(key=lambda x: x['score'], reverse=True)
        if len(relations) > 3:
            relations = relations[:3]
        return True, relations

    return False, []
def construct_relation_prune_prompt(question, entity_name, total_relations, args):
    prompt = extract_relation_prompt_wiki.format(
        question=question,
        entity_name=entity_name,
        total_relations=total_relations
    )
    return prompt

def relation_search_prune(entity_id, entity_name, pre_relations, pre_head, question, args, wiki_client):
    """对Neo4j的关系搜索 - 整合出度和入度关系"""
    try:
        # 获取实体的所有关系（包括出度和入度）
        relations = wiki_client.query_all("get_all_relations_of_an_entity", entity_id)

        # 合并所有关系，并标注方向
        all_relations_with_direction = []

        # 处理出度关系（head -> tail）
        for rel in relations['head']:
            all_relations_with_direction.append({
                'entity': entity_id,  # 重要：添加实体ID
                'entity_name': entity_name,  # 添加实体名称
                'relation': rel['label'],
                'direction': 'outgoing',  # 出度
                'head': True
            })

        # 处理入度关系（tail -> head）
        for rel in relations['tail']:
            all_relations_with_direction.append({
                'entity': entity_id,  # 重要：添加实体ID
                'entity_name': entity_name,  # 添加实体名称
                'relation': rel['label'],
                'direction': 'incoming',  # 入度
                'head': False
            })

        # 过滤无用关系
        if args.REMOVE_UNNECESSARY_REL:
            filtered_relations = []
            for rel_info in all_relations_with_direction:
                if not abandon_rels_neo4j(rel_info['relation']):
                    filtered_relations.append(rel_info)
            all_relations_with_direction = filtered_relations

        # 排除之前使用过的关系
        all_relations_with_direction = [
            rel_info for rel_info in all_relations_with_direction
            if rel_info['relation'] not in pre_relations
        ]

        if not all_relations_with_direction:
            return []

        # 按关系名称去重，确保LLM只看到每种关系类型一次
        # 例如：多个"包含"关系实例只保留一个，让LLM按关系类型选择
        unique_relations = {}
        for rel_info in all_relations_with_direction:
            rel_name = rel_info['relation']
            if rel_name not in unique_relations:
                unique_relations[rel_name] = rel_info
        all_relations_with_direction = list(unique_relations.values())

        # 构建提示词，让LLM选择关系和方向
        relation_list_str = "\n".join([
            f"{i + 1}. {rel_info['relation']} ({rel_info['direction']})"
            for i, rel_info in enumerate(all_relations_with_direction[:20])  # 限制显示数量
        ])

#         prompt = f"""你是一个知识图谱关系选择专家。请为以下问题选择最相关的关系及其方向：
#
# 问题：{question}
# 主题实体：{entity_name}
#
# 可用关系（带方向）：
# {relation_list_str}
#
# 请根据问题语义选择最相关的关系。注意：
# 1. "outgoing"表示从实体出发的关系（实体 -> 其他）
# 2. "incoming"表示指向实体的关系（其他 -> 实体）
#
# 例如：
# - 问题"谁拥有iPhone 15？"对于实体"iPhone 15"，应该选择"拥有 (incoming)"关系
# - 问题"张伟的爱好有哪些？"对于实体"张伟"，应该选择"喜欢 (outgoing)"关系
#
# 请按以下格式输出：
# {{关系名称 (方向) (评分：分数)}}
# 评分范围：0.0-1.0，1.0表示完全相关
#
# 请选择最多{args.WIDTH}个关系："""
        prompt = construct_relation_prune_prompt(question, entity_name, relation_list_str, args)

        result = run_llm(prompt, args.TEMPERATURE_EXPLORATION, args.MAX_LENGTH,
                         args.LLM_API_KEY, args.LLM_TYPE)

        retrieve_relations_with_scores = parse_relations_with_direction(
            result, all_relations_with_direction, args.WIDTH
        )

        # 添加LLM原始响应到结果中
        for rel in retrieve_relations_with_scores:
            rel['llm_response'] = result

        if retrieve_relations_with_scores:
            # 验证分数
            valid_relations = []
            for rel in retrieve_relations_with_scores:
                if rel.get('score', 0) > 0:
                    valid_relations.append(rel)

            if valid_relations:
                return valid_relations

        # 移除硬编码的后备方案，让LLM的判断成为唯一的关系选择方式
        # 如果LLM没有选择任何关系，返回空列表，表示当前实体没有相关关系
        return []

    except Exception as e:
        return []


def parse_relations_with_direction(llm_response, all_relations_with_direction, width):
    """解析LLM返回的关系和方向"""
    import re

    retrieve_relations = []

    # 首先尝试解析完整格式：{关系名称 (方向) (评分：分数)}
    pattern1 = r'\{([^{}]+?)\s*\((\w+)\)\s*\(评分：([\d.]+)\)\}'
    matches1 = re.findall(pattern1, llm_response)

    # 尝试解析简化格式：{关系名称 (评分：分数)}
    pattern2 = r'\{([^{}]+?)\s*\(评分：([\d.]+)\)\}'
    matches2 = re.findall(pattern2, llm_response)

    # 尝试解析最简格式：{关系名称 - 分数}
    pattern3 = r'\{([^{}]+?)\s*-\s*([\d.]+)\}'
    matches3 = re.findall(pattern3, llm_response)

    matches = matches1 if matches1 else (matches2 if matches2 else matches3)

    for match in matches[:width]:
        if len(match) == 3:  # 完整格式
            rel_name, direction, score_str = match
            direction = direction.lower()
        elif len(match) == 2:  # 简化格式
            rel_name, score_str = match
            # 根据问题类型推断方向
            direction = 'outgoing'  # 默认

        try:
            score = float(score_str)
            found = False
            for rel_info in all_relations_with_direction:
                if rel_info['relation'] == rel_name.strip():
                    if len(match) == 3:
                        llm_direction = direction.lower()
                        if rel_info['direction'] != llm_direction:
                            continue
                    retrieve_relations.append({
                        'entity': rel_info['entity'],
                        'entity_name': rel_info.get('entity_name', ''),
                        'relation': rel_name.strip(),
                        'score': score,
                        'head': rel_info.get('direction', 'outgoing') == 'outgoing',
                        'direction': rel_info.get('direction', 'outgoing')
                    })
                    found = True
                    break  # 找到匹配后退出循环

            if not found and len(match) >= 2:
                matched_direction = direction if len(match) == 3 else 'outgoing'
                retrieve_relations.append({
                    'entity': all_relations_with_direction[0]['entity'] if all_relations_with_direction else '',
                    'entity_name': all_relations_with_direction[0].get('entity_name', '') if all_relations_with_direction else '',
                    'relation': rel_name.strip(),
                    'score': float(score_str),
                    'head': matched_direction == 'outgoing',
                    'direction': matched_direction
                })

        except (ValueError, IndexError) as e:
            continue

    # 如果没有找到任何关系，直接返回空列表
    # 移除硬编码的启发式方法，让LLM的判断成为唯一的关系选择方式
    return retrieve_relations


def abandon_rels_neo4j(relation):
    """Neo4j专用的关系过滤 - 只过滤明显无用的技术型关系，让LLM判断其他关系的相关性"""
    relation_lower = relation.lower()

    # 只过滤明显无用的技术型元数据关系
    useless_relation_list = [
        "id", "internal_id", "element_id",
        "_id", "created", "updated", "timestamp"
    ]

    if relation_lower in useless_relation_list:
        return True

    # 移除短关系名过滤，让LLM来判断所有有意义的关系
    # 原代码：if len(relation) <= 2 and not any(ch in relation for ch in ['在', '于', '给', '到', '有', '是', '为']):
    # 理由：短关系名如"包含"、"属于"、"组成"等可能是非常重要的关系
    
    return False
def del_all_unknown_entity(entity_candidates_id, entity_candidates_name):
    if len(entity_candidates_name) == 1 and entity_candidates_name[0] == "N/A":
        return entity_candidates_id, entity_candidates_name

    new_candidates_id = []
    new_candidates_name = []
    for i, candidate in enumerate(entity_candidates_name):
        if candidate != "N/A":
            new_candidates_id.append(entity_candidates_id[i])
            new_candidates_name.append(candidate)

    return new_candidates_id, new_candidates_name


def all_zero(topn_scores):
    return all(score == 0 for score in topn_scores)


def entity_search(entity, relation, wiki_client, head):
    direction = "outgoing" if head else "incoming"
    try:
        entities = wiki_client.query_all("get_tail_entities_given_head_and_relation", entity, relation, direction)
        entities_set = entities.get('tail', [])

        if not entities_set:
            return [], []

        id_list = [item['qid'] for item in entities_set]
        name_list = [item['label'] if item['label'] != "N/A" else "未知实体" for item in entities_set]

        return id_list, name_list

    except Exception as e:
        return [], []


def construct_entity_score_prompt(question, relation, entity_candidates):
    # 将实体列表转换为分号分隔的字符串
    entities_str = "; ".join(entity_candidates)
    return score_entity_candidates_prompt_wiki.format(question, relation, entities_str)

def entity_score(question, entity_candidates_id, entity_candidates, score, relation, args):
    if len(entity_candidates) == 1:
        return [score], entity_candidates, entity_candidates_id
    if len(entity_candidates) == 0:
        return [0.0], entity_candidates, entity_candidates_id
    
    # make sure the id and entity are in the same order
    zipped_lists = sorted(zip(entity_candidates, entity_candidates_id))
    entity_candidates, entity_candidates_id = zip(*zipped_lists)
    entity_candidates = list(entity_candidates)
    entity_candidates_id = list(entity_candidates_id)

    prompt = construct_entity_score_prompt(question, relation, entity_candidates)

    result = run_llm(prompt, args.TEMPERATURE_EXPLORATION, args.MAX_LENGTH, args.LLM_API_KEY, args.LLM_TYPE)
    entity_scores = clean_scores(result, entity_candidates)
    if all_zero(entity_scores):
        return [1/len(entity_candidates) * score] * len(entity_candidates), entity_candidates, entity_candidates_id
    else:
        return [float(x) * score for x in entity_scores], entity_candidates, entity_candidates_id


def update_history(entity_candidates, entity, scores, entity_candidates_id, total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head, value_flag):
    if value_flag:
        scores = [1/len(entity_candidates) * entity['score']]
    candidates_relation = [entity['relation']] * len(entity_candidates)
    topic_entities = [entity['entity']] * len(entity_candidates)
    head_num = [entity['head']] * len(entity_candidates)
    total_candidates.extend(entity_candidates)
    total_scores.extend(scores)
    total_relations.extend(candidates_relation)
    total_entities_id.extend(entity_candidates_id)
    total_topic_entities.extend(topic_entities)
    total_head.extend(head_num)
    return total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head


def half_stop(question, cluster_chain_of_entities, depth, args):
    answer = generate_answer(question, cluster_chain_of_entities, args)
    save_2_jsonl(question, answer, cluster_chain_of_entities, file_name=args.dataset)


def generate_answer(question, cluster_chain_of_entities, args): 
    prompt = answer_prompt_wiki + question + '\n'
    chain_prompt = '\n'.join([', '.join([str(x) for x in chain]) for sublist in cluster_chain_of_entities for chain in sublist])
    prompt += "\n知识三元组：" + chain_prompt + '\n回答：'
    result = run_llm(prompt, args.TEMPERATURE_REASONING, args.MAX_LENGTH, args.LLM_API_KEY, args.LLM_TYPE)
    return result


def entity_prune(total_entities_id, total_relations, total_candidates, total_topic_entities, total_head, total_scores,
                 args, wiki_client, entity_linker=None):

    zipped = list(
        zip(total_entities_id, total_relations, total_candidates, total_topic_entities, total_head, total_scores))
    
    # 按关系类型分组，确保每种关系都有机会被保留
    relations_dict = {}
    for e_id, rel, cand, top, head, score in zipped:
        if rel not in relations_dict:
            relations_dict[rel] = []
        relations_dict[rel].append((e_id, rel, cand, top, head, score))
    
    # 每种关系取最多 NUM_RETAIN_ENTITY 个实体（按分数排序）
    filtered_by_relation = []
    for rel, items in relations_dict.items():
        # 按分数排序
        sorted_items = sorted(items, key=lambda x: x[5], reverse=True)
        # 每种关系最多保留 NUM_RETAIN_ENTITY 个
        filtered_by_relation.extend(sorted_items[:args.NUM_RETAIN_ENTITY])
    
    # 再按分数排序取前 WIDTH 个，保证整体质量
    filtered_by_relation.sort(key=lambda x: x[5], reverse=True)
    sorted_zipped = filtered_by_relation[:args.WIDTH]
    
    if not sorted_zipped:
        return False, [], [], [], []
    
    sorted_entities_id, sorted_relations, sorted_candidates, sorted_topic_entities, sorted_head, sorted_scores = \
        [x[0] for x in sorted_zipped], [x[1] for x in sorted_zipped], [x[2] for x in sorted_zipped], \
        [x[3] for x in sorted_zipped], [x[4] for x in sorted_zipped], [x[5] for x in sorted_zipped]

    merged_list = list(zip(sorted_entities_id, sorted_relations, sorted_candidates, sorted_topic_entities, sorted_head, sorted_scores))
    filtered_list = [(id, rel, ent, top, hea, score) for id, rel, ent, top, hea, score in merged_list if score != 0]


    if len(filtered_list) == 0:
        return False, [], [], [], []

    entities_id, relations, candidates, tops, heads, scores = map(list, zip(*filtered_list))
    tops_labels = []
    tops_with_properties = []
    for entity_id in tops:
        label_result = wiki_client.query_all("qid2label", entity_id)
        if label_result and label_result != "Not Found!" and len(label_result) > 0:
            try:
                label = next(iter(label_result))
                tops_labels.append(label)
                # 获取实体的完整属性信息
                properties = {}
                try:
                    if entity_linker:
                        entity_info = entity_linker.get_node_full_info(entity_id)
                        if entity_info:
                            properties = entity_info.get("properties", {})
                except:
                    pass
                tops_with_properties.append({
                    "name": label,
                    "id": entity_id,
                    "properties": properties
                })
            except:
                tops_labels.append("未知实体")
                tops_with_properties.append({
                    "name": "未知实体",
                    "id": entity_id,
                    "properties": {}
                })
        else:
            tops_labels.append("未知实体")
            tops_with_properties.append({
                "name": "未知实体",
                "id": entity_id,
                "properties": {}
            })
    # 构建三元组链
    triple_list = []
    for i in range(len(candidates)):
        try:
            if i < len(tops_labels) and i < len(relations) and i < len(heads):
                head = tops_labels[i] if i < len(tops_labels) else "未知实体"
                rel = relations[i] if i < len(relations) else "未知关系"
                tail = candidates[i] if i < len(candidates) else "未知实体"
                is_head = heads[i] if i < len(heads) else True
                
                # 获取候选实体的属性信息
                tail_id = entities_id[i] if i < len(entities_id) else None
                tail_properties = {}
                if tail_id and tail_id != "[FINISH_ID]":
                    try:
                        if entity_linker:
                            entity_info = entity_linker.get_node_full_info(tail_id)
                            if entity_info:
                                tail_properties = entity_info.get("properties", {})
                    except:
                        pass
                
                # 构建候选实体的完整信息
                tail_entity = {
                    "name": tail,
                    "id": tail_id if tail_id else "未知ID",
                    "properties": tail_properties
                }
                
                # 根据关系方向正确构建三元组
                # is_head=True 表示出度关系：主题实体 -> 候选实体
                # is_head=False 表示入度关系：候选实体 -> 主题实体
                if is_head:
                    triple = (tops_with_properties[i] if i < len(tops_with_properties) else {"name": "未知实体", "properties": {}}, rel, tail_entity)
                else:
                    triple = (tail_entity, rel, tops_with_properties[i] if i < len(tops_with_properties) else {"name": "未知实体", "properties": {}})
                triple_list.append(triple)
        except Exception as e:
            continue

    # 直接返回三元组列表
    if triple_list:
        return True, triple_list, entities_id[:len(triple_list)], relations[:len(triple_list)], heads[:len(triple_list)]
    else:
        return False, [], [], [], []

# reasoning 函数已移除，因为智能体模型不需要后端进行最终推理
# 所有推理和答案生成由 Claude Code 完成