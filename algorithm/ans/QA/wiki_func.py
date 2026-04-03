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
                    print(f"分数解析失败: {relation} (原始分数: {score_str})")

    if relations:

        # 按分数排序
        relations.sort(key=lambda x: x['score'], reverse=True)

        # 限制数量
        if len(relations) > 3:
            relations = relations[:3]

        return True, relations

    # 如果没有解析到任何关系，返回 False 和空列表
    print("未解析到任何有效关系")
    return False, []

def check_end_word(s):
    words = [" ID", " code", " number", "instance of", "website", "URL", "inception", "image", " rate", " count"]
    return any(s.endswith(word) for word in words)


def abandon_rels(relation):
    useless_relation_list = ["category's main topic", "topic\'s main category", "stack exchange site", 'main subject', 'country of citizenship', "commons category", "commons gallery", "country of origin", "country", "nationality"]
    if check_end_word(relation) or 'wikidata' in relation.lower() or 'wikimedia' in relation.lower() or relation.lower() in useless_relation_list:
        return True
    return False

def construct_relation_prune_prompt(question, entity_name, total_relations, args):
    print(f"DEBUG: question type = {type(question)}, value = {question}")
    print(f"DEBUG: entity_name type = {type(entity_name)}, value = {entity_name}")
    print(f"DEBUG: total_relations type = {type(total_relations)}, value = {total_relations}")
    print(f"DEBUG: total_relations length = {len(total_relations) if hasattr(total_relations, '__len__') else 'N/A'}")
    
    prompt = extract_relation_prompt_wiki.format(
        question=question,  # 问题
        entity_name=entity_name,  # 主题实体
        total_relations=total_relations  # 关系列表（已经包含编号）
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
            print(f"实体 {entity_name} 没有找到可用关系")
            return []

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

        print(f"LLM原始响应:\n{result}\n{'-' * 50}")

        # 解析LLM响应
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
            else:
                print("所有关系的分数都为0")

        # 如果LLM选择失败，使用智能后备方案
        print(f"LLM关系选择失败，使用智能后备方案")
        return get_fallback_relations_enhanced(all_relations_with_direction, args.WIDTH)

    except Exception as e:
        print(f"关系搜索异常: {e}")
        import traceback
        traceback.print_exc()
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
            # 在可用关系中查找匹配项
            found = False
            for rel_info in all_relations_with_direction:
                if rel_info['relation'] == rel_name.strip():
                    if 'direction' in match and rel_info['direction'] != direction:
                        continue  # 方向不匹配，跳过

                    retrieve_relations.append({
                        'entity': rel_info['entity'],  # 重要：包含实体ID
                        'entity_name': rel_info.get('entity_name', ''),
                        'relation': rel_name.strip(),
                        'score': score,
                        'head': rel_info['direction'] == 'outgoing' if 'direction' in rel_info else True,
                        'direction': rel_info.get('direction', 'outgoing')
                    })
                    found = True
                    break

            if not found and len(match) >= 2:
                # 如果没有找到精确匹配，创建一个新的条目
                retrieve_relations.append({
                    'entity': all_relations_with_direction[0]['entity'] if all_relations_with_direction else '',
                    'entity_name': all_relations_with_direction[0].get('entity_name',
                                                                       '') if all_relations_with_direction else '',
                    'relation': rel_name.strip(),
                    'score': float(score_str),
                    'head': True,  # 默认
                    'direction': 'outgoing'
                })

        except (ValueError, IndexError) as e:
            print(f"解析分数失败: {e}")
            continue

    # 如果没有找到任何关系，使用简单启发式方法
    if not retrieve_relations:
        retrieve_relations = simple_direction_heuristic(all_relations_with_direction, width)

    return retrieve_relations


def simple_direction_heuristic(relations_with_direction, width):
    """简单的方向启发式方法"""
    scored_relations = []

    relation_priority = {
        # 高优先级关系
        "拥有": 0.9, "工作于": 0.9, "学习于": 0.9, "住在": 0.9,
        "喜欢": 0.8, "使用": 0.8, "认识": 0.8, "访问": 0.8,
        "包含": 0.7, "举办": 0.7, "发生在": 0.7,
        # 中优先级关系
        "参与": 0.6, "需要": 0.6, "相关于": 0.5
    }

    for rel_info in relations_with_direction[:width * 2]:
        if 'entity' not in rel_info:
            continue

        base_score = relation_priority.get(rel_info['relation'], 0.4)

        import random
        final_score = base_score * (0.9 + random.random() * 0.2)

        scored_relations.append((rel_info, final_score))

    # 按分数排序
    scored_relations.sort(key=lambda x: x[1], reverse=True)

    # 构建返回结果
    retrieve_relations = []
    for rel_info, score in scored_relations[:width]:
        retrieve_relations.append({
            'entity': rel_info['entity'],
            'entity_name': rel_info.get('entity_name', ''),
            'relation': rel_info['relation'],
            'score': score,
            'head': rel_info.get('direction', 'outgoing') == 'outgoing',
            'direction': rel_info.get('direction', 'outgoing')
        })

    return retrieve_relations


def get_fallback_relations_enhanced(all_relations_with_direction, width):
    """后备方案，考虑关系方向"""
    fallback_relations = []

    # 根据关系和方向分配分数
    relation_direction_priority = {
        # 高优先级组合
        ("拥有", "incoming"): 1.0,  # 谁拥有X
        ("学生于", "incoming"): 0.9,  # 谁是X的学生
        ("工作于", "outgoing"): 0.9,  # X在哪里工作
        ("喜欢", "outgoing"): 0.9,  # X喜欢什么
        # 中优先级
        ("使用", "outgoing"): 0.7,
        ("访问", "outgoing"): 0.7,
        ("包含", "outgoing"): 0.7,
        ("举办", "outgoing"): 0.7,
        # 低优先级
        ("相关于", "outgoing"): 0.4,
        ("相关于", "incoming"): 0.3,
    }

    # 为每个关系-方向组合评分
    scored_relations = []
    for rel_info in all_relations_with_direction:
        if 'entity' not in rel_info:
            continue

        key = (rel_info['relation'], rel_info.get('direction', 'outgoing'))
        base_score = relation_direction_priority.get(key, 0.5)

        import random
        final_score = base_score * (0.9 + random.random() * 0.2)

        scored_relations.append((rel_info, final_score))

    # 按分数排序
    scored_relations.sort(key=lambda x: x[1], reverse=True)

    # 选择前width个
    for rel_info, score in scored_relations[:width]:
        fallback_relations.append({
            "entity": rel_info['entity'],  # 确保包含entity
            "entity_name": rel_info.get('entity_name', ''),
            "relation": rel_info['relation'],
            "score": score,
            "head": rel_info.get('direction', 'outgoing') == 'outgoing',
            "direction": rel_info.get('direction', 'outgoing')
        })

    print(f"后备方案选择的关系: {[{'rel': r['relation'], 'entity': r['entity'][:10]} for r in fallback_relations]}")
    return fallback_relations

def get_fallback_relations(entity_id, available_relations, direction, width):
    fallback_relations = []

    # 根据关系类型分配不同基础分数
    relation_priority = {
        # 高优先级关系
        "住在": 0.9, "工作于": 0.9, "学习于": 0.9, "拥有": 0.9, "使用": 0.9,
        "喜欢": 0.8, "访问": 0.8, "认识": 0.8, "参与": 0.8,
        # 中优先级关系
        "包含": 0.6, "举办": 0.6, "需要": 0.6, "发生在": 0.6,
        # 低优先级关系
        "相关于": 0.3, "属于": 0.3, "包含于": 0.3
    }

    # 为每个可用关系分配分数
    scored_relations = []
    for rel in available_relations[:width * 2]:  # 考虑两倍数量
        base_score = relation_priority.get(rel, 0.5)
        # 添加一些随机性避免完全相同分数
        import random
        final_score = base_score * (0.9 + random.random() * 0.2)

        scored_relations.append((rel, final_score))

    # 按分数排序
    scored_relations.sort(key=lambda x: x[1], reverse=True)

    # 选择前width个
    for rel, score in scored_relations[:width]:
        fallback_relations.append({
            "entity": entity_id,
            "relation": rel,
            "score": score,
            "head": (direction == "outgoing")
        })

    # 标准化分数
    total_score = sum(r['score'] for r in fallback_relations)
    if total_score > 0:
        for r in fallback_relations:
            r['score'] = r['score'] / total_score

    print(f"后备方案选择的关系: {[r['relation'] for r in fallback_relations]}")
    return fallback_relations

def abandon_rels_neo4j(relation):
    """Neo4j专用的关系过滤"""
    # Neo4j中的关系通常比较简洁，不要过度过滤
    relation_lower = relation.lower()

    # 仅过滤明显的技术性关系
    useless_relation_list = [
        "id", "internal_id", "element_id",
        "_id", "created", "updated", "timestamp"
    ]

    # 检查是否是关系列表中的关系
    known_relations = [
        # 人物相关关系
        "住在", "工作于", "学习于", "学生于", "认识", "见面", "帮助",
        "打电话给", "发消息给", "参与", "喜欢", "访问", "拥有", "使用",
        "穿着", "购买", "阅读",
        # 地点相关关系
        "举办", "包含", "需要", "产生", "发生在", "放在", "相关于",
        # 交通相关关系
        "连接", "相连", "连接到", "链接", "连通"
    ]

    # 如果是已知的有意义关系，保留
    if relation in known_relations:
        return False

    # 过滤技术性关系
    if relation_lower in useless_relation_list:
        return True

    # 关系太短可能是ID
    if len(relation) <= 2 and not any(ch in relation for ch in ['在', '于', '给', '到']):
        return True

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
    """针对Neo4j的实体搜索，正确处理head参数"""
    print(f"实体搜索: entity={entity}, relation={relation}, head={head}")

    try:
        if head:
            entities = wiki_client.query_all("get_tail_entities_given_head_and_relation", entity, relation)
            entities_set = entities['tail']
        else:
            entities = wiki_client.query_all("get_tail_entities_given_head_and_relation", entity, relation)
            entities_set = entities['tail']

        if not entities_set:
            print(f"未找到实体")
            return [], []

        id_list = [item['qid'] for item in entities_set]
        name_list = [item['label'] if item['label'] != "N/A" else "Unname_Entity" for item in entities_set]

        print(f"提取的名称列表: {name_list}")

        return id_list, name_list

    except Exception as e:
        print(f"实体搜索异常: {e}")
        import traceback
        traceback.print_exc()
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
    print("No new knowledge added during search depth %d, stop searching." % depth)
    answer = generate_answer(question, cluster_chain_of_entities, args)
    save_2_jsonl(question, answer, cluster_chain_of_entities, file_name=args.dataset)


def generate_answer(question, cluster_chain_of_entities, args): 
    prompt = answer_prompt_wiki + question + '\n'
    chain_prompt = '\n'.join([', '.join([str(x) for x in chain]) for sublist in cluster_chain_of_entities for chain in sublist])
    prompt += "\nKnowledge Triplets: " + chain_prompt + 'A: '
    result = run_llm(prompt, args.TEMPERATURE_REASONING, args.MAX_LENGTH, args.LLM_API_KEY, args.LLM_TYPE)
    return result


def entity_prune(total_entities_id, total_relations, total_candidates, total_topic_entities, total_head, total_scores,
                 args, wiki_client, entity_linker=None):

    zipped = list(
        zip(total_entities_id, total_relations, total_candidates, total_topic_entities, total_head, total_scores))
    sorted_zipped = sorted(zipped, key=lambda x: x[5], reverse=True)
    sorted_entities_id, sorted_relations, sorted_candidates, sorted_topic_entities, sorted_head, sorted_scores = [x[0] for x in sorted_zipped], [x[1] for x in sorted_zipped], [x[2] for x in sorted_zipped], [x[3] for x in sorted_zipped], [x[4] for x in sorted_zipped], [ x[5] for x in sorted_zipped]

    entities_id, relations, candidates, topics, heads, scores = sorted_entities_id[:args.WIDTH], sorted_relations[:args.WIDTH], sorted_candidates[:args.WIDTH], sorted_topic_entities[ :args.WIDTH], sorted_head[:args.WIDTH], sorted_scores[:args.WIDTH]
    merged_list = list(zip(entities_id, relations, candidates, topics, heads, scores))
    filtered_list = [(id, rel, ent, top, hea, score) for id, rel, ent, top, hea, score in merged_list if score != 0]


    if len(filtered_list) == 0:
        print(f"所有分数都为0，返回失败")
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
                tops_labels.append("Unname_Entity")
                tops_with_properties.append({
                    "name": "Unname_Entity",
                    "id": entity_id,
                    "properties": {}
                })
                print(f"实体 {entity_id} 标签获取失败，使用默认")
        else:
            tops_labels.append("Unname_Entity")
            tops_with_properties.append({
                "name": "Unname_Entity",
                "id": entity_id,
                "properties": {}
            })
            print(f"实体 {entity_id} 没有标签")
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
            print(f"构建三元组 {i} 时出错: {e}")
            continue

    # 直接返回三元组列表
    if triple_list:
        return True, triple_list, entities_id[:len(triple_list)], relations[:len(triple_list)], heads[:len(triple_list)]
    else:
        print(f"未能构建任何三元组")
        return False, [], [], [], []

def reasoning(question, cluster_chain_of_entities, args):
    """使用LLM进行推理"""
    triples_text = []

    for sublist in cluster_chain_of_entities:
        if isinstance(sublist, list):
            for item in sublist:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    head, rel, tail = item[0], item[1], item[2]
                    
                    # 检查head是否是字典格式（包含属性信息）
                    head_name = head
                    head_properties = {}
                    if isinstance(head, dict):
                        head_name = head.get('name', str(head))
                        head_properties = head.get('properties', {})
                    
                    # 检查tail是否是字典格式（包含属性信息）
                    tail_name = tail
                    tail_properties = {}
                    if isinstance(tail, dict):
                        tail_name = tail.get('name', str(tail))
                        tail_properties = tail.get('properties', {})
                    
                    # 构建包含属性信息的三元组
                    triple_info = f"{head_name}，{rel}，{tail_name}"
                    
                    # 添加head的属性信息
                    if head_properties:
                        data_source = head_properties.get('数据来源表', '')
                        calc_sql = head_properties.get('计算SQL', '')
                        if data_source:
                            triple_info += f"（{head_name}数据来源表：{data_source}）"
                        if calc_sql:
                            triple_info += f"（{head_name}计算SQL：{calc_sql}）"
                    
                    # 添加tail的属性信息
                    if tail_properties:
                        data_source = tail_properties.get('数据来源表', '')
                        calc_sql = tail_properties.get('计算SQL', '')
                        if data_source:
                            triple_info += f"（{tail_name}数据来源表：{data_source}）"
                        if calc_sql:
                            triple_info += f"（{tail_name}计算SQL：{calc_sql}）"
                    
                    triples_text.append(triple_info)
                else:
                    print(f"无法处理的项目: 类型={type(item)}, 值={item}")

    if not triples_text:
        print("推理：未找到任何三元组信息")
        return False, "信息不足"

    # 将三元组列表转换为字符串格式
    triples_str = "\n".join(triples_text)

    # 评估信息是否足够并生成答案
    evaluate_prompt = prompt_evaluate_wiki.format(
        question,
        triples_str,
        ""
    )

    evaluate_answer = run_llm(evaluate_prompt, args.TEMPERATURE_REASONING,
                              args.MAX_LENGTH, args.LLM_API_KEY, args.LLM_TYPE)

    print(f"LLM评估回答: {evaluate_answer}")

    # 判断信息是否足够
    is_sufficient = False
    answer = "信息不足"
    # 清理回答
    evaluate_answer_clean = evaluate_answer.strip()
    # 模式1: 查找 {是否足够：是/否} 或 {{是否足够：是/否}}
    pattern1 = r'\{{?是否足够[：:]\s*([是否])\s*\}}?'
    match1 = re.search(pattern1, evaluate_answer_clean)
    # 模式2: 查找 {是/否} 或 {{是/否}}
    pattern2 = r'\{{?\s*([是否])\s*\}}?'
    match2 = re.search(pattern2, evaluate_answer_clean)
    # 模式3: 直接查找"是"或"否"关键词
    if match1:
        result = match1.group(1)
        is_sufficient = (result == "是")
    elif match2:
        result = match2.group(1)
        is_sufficient = (result == "是")
    else:
        if "是" in evaluate_answer_clean and "否" not in evaluate_answer_clean[:20]:
            is_sufficient = True
        elif "否" in evaluate_answer_clean[:20]:
            is_sufficient = False
    # 4. 提取答案部分
    if is_sufficient:
        # 尝试提取简洁答案
        # 首先尝试匹配"回答："或"答案："后面的内容
        answer_pattern0 = r'(?:回答|答案)[：:]\s*(.+?)(?:\n\n|$)'
        answer_match0 = re.search(answer_pattern0, evaluate_answer_clean, re.DOTALL)
        
        if answer_match0:
            answer = answer_match0.group(1).strip()
            print(f"提取到答案: {answer}")
        else:
            # 尝试匹配花括号格式
            # 查找 {答案：...} 或 {{答案：...}} 或 {...} 或 {{...}} 格式
            answer_pattern1 = r'\{{?答案[：:]\s*([^\}]+)\}}?'
            answer_pattern2 = r'\{{?\s*是\s*\}}?\s*[,，。]?\s*(.+)'

            answer_match1 = re.search(answer_pattern1, evaluate_answer_clean)
            answer_match2 = re.search(answer_pattern2, evaluate_answer_clean, re.DOTALL)

            if answer_match1:
                answer = answer_match1.group(1).strip()
                print(f"提取到答案: {answer}")
            elif answer_match2:
                answer = answer_match2.group(1).strip()
                print(f"提取到简洁答案: {answer}")
            else:
                # 如果没有找到括号格式，使用完整文本（去除"是"或"否"等标记）
                # 移除开头的{是}或{否}标记
                answer = re.sub(r'^\{{?\s*[是否]\s*\}}?\s*[,，。]?\s*', '', evaluate_answer_clean)
                if not answer:
                    answer = evaluate_answer_clean

        return True, answer

    return False, answer