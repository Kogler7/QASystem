"""Prompt generation and parsing utilities."""

import re
import json


def clean_intent(intent):
    """Clean and normalize intent labels."""
    intent = intent.lower().strip()
    intent = intent.replace(' ', '_')
    # Remove any trailing punctuation or quotes
    intent = intent.rstrip('.,;:!?\'"')
    return intent


def parse_json_output(output_text):
    """
    Try to parse JSON output from LLM.
    Supports multiple JSON formats.
    
    Returns:
        list: Parsed data as list of dicts, or None if parsing failed
    """
    try:
        # Method 1: Direct JSON parsing
        text = output_text.strip()
        if text.startswith('[') and text.endswith(']'):
            data = json.loads(text)
            if isinstance(data, list) and len(data) > 0:
                # Validate structure
                if all(isinstance(item, dict) and 'intent' in item for item in data):
                    print(f" JSON解析成功 (方法1-直接解析): {len(data)}条")
                    return data
    except json.JSONDecodeError:
        pass
    except Exception as e:
        pass
    
    # Method 2: Extract from code blocks
    patterns = [
        r'```json\s*([\s\S]*?)\s*```',  # ```json ... ```
        r'```\s*([\s\S]*?)\s*```',       # ``` ... ```
        r'\[\s*\{[\s\S]*?\}\s*\]'        # [ {...} ]
    ]
    
    for i, pattern in enumerate(patterns):
        try:
            matches = re.findall(pattern, output_text, re.DOTALL)
            for match in matches:
                try:
                    # For the third pattern, match is already the full JSON
                    json_str = match if i == 2 else match
                    data = json.loads(json_str)
                    if isinstance(data, list) and len(data) > 0:
                        if all(isinstance(item, dict) and 'intent' in item for item in data):
                            print(f" JSON解析成功 (方法{i+2}): {len(data)}条")
                            return data
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue
    
    return None


def parse_output(output_text):
    """
    Parse model output to extract ID, utterance, and intent.
    Version 2.0: Enhanced with JSON parsing and more regex strategies.
    
    Parsing priority:
    1. JSON format (best)
    2. Standard format: ID: X, Utterance: Y, Intent: Z
    3. Numbered list: 1. Utterance: Y, Intent: Z
    4. Flexible format with various delimiters
    5. Table format
    6. Line-by-line fallback
    """
    parsed_output = []
    
    try:
        # ===== PRIORITY 1: JSON Parsing =====
        json_result = parse_json_output(output_text)
        if json_result:
            # Normalize JSON output to our format
            for item in json_result:
                parsed_output.append({
                    'id': int(item.get('id', 0)),
                    'utterance': item.get('utterance', '').strip(),
                    'intent': clean_intent(item.get('intent', '')),
                })
            return parsed_output
        
        # ===== PRIORITY 2-7: Regex Strategies =====
        strategies = [
            # Strategy 2: Standard format "ID: X, Utterance: Y, Intent: Z"
            (
                r"ID:\s*(\d+),?\s*Utterance:\s*(.*?),?\s*Intent:\s*(.+?)(?=\n|ID:|$)",
                3,
                "标准格式(ID: X, Utterance: Y, Intent: Z)"
            ),
            
            # Strategy 3: Numbered list format "1. Utterance: Y, Intent: Z" (Llama common)
            (
                r"(\d+)\.\s+Utterance:\s*(.*?),?\s*Intent:\s*(.+?)(?=\n|\d+\.|$)",
                3,
                "编号列表格式(1. Utterance: Y, Intent: Z)"
            ),
            
            # Strategy 4: Flexible format with various delimiters
            (
                r"(?:ID:?\s*)?(\d+)[\.\,\:]?\s+(?:Utterance:?\s*)?(.*?)[,:]?\s+Intent:?\s*(.+?)(?=\n|$)",
                3,
                "灵活格式(支持多种分隔符)"
            ),
            
            # Strategy 5: Table format "| ID | Utterance | Intent |"
            (
                r"\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|",
                3,
                "表格格式(| ID | Utterance | Intent |)"
            ),
            
            # Strategy 6: Simplified - ID and Intent only
            (
                r"(?:ID:?\s*)?(\d+)[\.\,\:]?\s+Intent:?\s*([^\n,]+)",
                2,
                "简化格式(仅ID和Intent)"
            ),
            
            # Strategy 7: Very relaxed - just find intent labels
            (
                r"Intent:?\s*([a-z_]+)",
                1,
                "极简格式(仅Intent)"
            ),
        ]
        
        for strategy_num, (pattern, num_groups, description) in enumerate(strategies, start=2):
            matches = re.findall(pattern, output_text, re.IGNORECASE | re.MULTILINE)
            
            if matches and len(matches) > 0:
                print(f" 策略{strategy_num}成功: {description} - 解析{len(matches)}条")
                
                for idx, match in enumerate(matches):
                    if num_groups == 3:
                        parsed_output.append({
                            'id': int(match[0]) if match[0].isdigit() else idx,
                            'utterance': match[1].strip(),
                            'intent': clean_intent(match[2]),
                        })
                    elif num_groups == 2:
                        parsed_output.append({
                            'id': int(match[0]) if match[0].isdigit() else idx,
                            'utterance': '',
                            'intent': clean_intent(match[1]),
                        })
                    elif num_groups == 1:
                        parsed_output.append({
                            'id': idx,
                            'utterance': '',
                            'intent': clean_intent(match),
                        })
                
                if parsed_output:
                    if num_groups < 3:
                        print(f"  Warning: Utterances may be missing in strategy {strategy_num}")
                    return parsed_output
        
        # All strategies failed
        print(f" Error: Could not parse output with any strategy.")
        print(f"Output preview (first 500 chars):\n{output_text[:500]}")
        return []
        
    except Exception as e:
        print(f" Error in parse_output(): {str(e)}")
        print(f"Output preview (first 500 chars):\n{output_text[:500]}")
        import traceback
        traceback.print_exc()
        return []


def get_prompt_template():
    """Get the enhanced prompt template for intent classification with JSON output."""
    return """{prompt}

 OUTPUT FORMAT (CRITICAL):
You MUST output ONLY a valid JSON array. NO explanations, headers, or other text.

Required JSON format:
[
  {{"id": 0, "utterance": "text here", "intent": "intent_name"}},
  {{"id": 1, "utterance": "text here", "intent": "intent_name"}}
]

Rules:
1. Output ONLY the JSON array
2. Each object must have: id (number), utterance (string), intent (string)
3. Intent names must be lowercase with underscores (e.g., pin_blocked)
4. Use the same ID from the test example

Example:
[
  {{"id": 0, "utterance": "my card is blocked", "intent": "pin_blocked"}},
  {{"id": 1, "utterance": "where is my refund", "intent": "refund_not_showing_up"}}
]

CONTEXT EXAMPLES:
{few_shot_examples}

TEST EXAMPLES (to predict):
{test_examples}

 Output ONLY the JSON array NOW (no other text): """
