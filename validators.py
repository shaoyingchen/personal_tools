"""
验证器模块 - 处理输入验证和数据清理
"""
import re
import json
from werkzeug.exceptions import BadRequest

def validate_prompt_name(name):
    """验证提示词名称"""
    if not name or not name.strip():
        raise BadRequest("提示词名称不能为空")
    if len(name.strip()) > 200:
        raise BadRequest("提示词名称不能超过200个字符")
    return name.strip()

def validate_prompt_content(content):
    """验证提示词内容"""
    if not content or not content.strip():
        raise BadRequest("提示词内容不能为空")
    if len(content) > 100000:  # 100KB限制
        raise BadRequest("提示词内容过长")
    return content.strip()

def validate_tags(tags_list):
    """验证标签列表"""
    if not tags_list:
        return []

    valid_tags = []
    for tag in tags_list:
        tag = tag.strip()
        if tag and len(tag) <= 50:  # 标签长度限制
            # 移除无效字符
            tag = re.sub(r'[<>"]', '', tag)
            if tag:
                valid_tags.append(tag)

    return list(set(valid_tags))  # 去重

def validate_color(color):
    """验证颜色值"""
    if not color:
        return None

    color = color.strip().lower()
    # 支持简写和完整格式
    if re.match(r'^#[0-9a-f]{3}$', color):
        return color
    if re.match(r'^#[0-9a-f]{6}$', color):
        return color

    return None

def validate_version(version):
    """验证版本号格式"""
    if not version:
        return "1.0.0"

    version = version.strip()
    if re.match(r'^\d+\.\d+\.\d+$', version):
        return version

    return "1.0.0"

def validate_source(source):
    """验证来源"""
    if not source:
        return ""

    source = source.strip()
    if len(source) > 100:
        source = source[:100] + "..."

    return source

def validate_notes(notes):
    """验证备注"""
    if not notes:
        return ""

    notes = notes.strip()
    if len(notes) > 1000:
        notes = notes[:1000] + "..."

    return notes

def sanitize_search_query(query):
    """清理搜索查询"""
    if not query:
        return ""

    # 移除特殊字符，保留中文、英文、数字、空格
    query = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', query)
    # 移除多余空格
    query = re.sub(r'\s+', ' ', query).strip()

    return query[:100]  # 限制长度

def validate_pagination_params(page, per_page):
    """验证分页参数"""
    try:
        page = max(1, int(page))
        per_page = max(1, min(100, int(per_page)))  # 限制每页最多100条
        return page, per_page
    except:
        return 1, 20

def validate_json_field(field_value, field_name=""):
    """验证JSON字段"""
    if not field_value:
        return None

    try:
        if isinstance(field_value, str):
            json.loads(field_value)
        return field_value
    except json.JSONDecodeError:
        raise BadRequest(f"{field_name}字段格式不正确，请使用有效的JSON格式")

def validate_sort_field(sort_field, allowed_fields):
    """验证排序字段"""
    if not sort_field or sort_field not in allowed_fields:
        return allowed_fields[0]  # 默认排序字段
    return sort_field

def validate_order_direction(order):
    """验证排序方向"""
    return order.lower() if order.lower() in ['asc', 'desc'] else 'desc'