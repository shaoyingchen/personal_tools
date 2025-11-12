#!/usr/bin/env python3
"""
提示词智能导入工具
自动扫描指定目录的提示词文件，分析内容并进行智能分类导入
"""

import os
import re
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import get_db, init_database, get_category_by_name, get_category_by_id

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class PromptImporter:
    """提示词智能导入器"""

    def __init__(self, source_dir: str):
        self.source_dir = Path(source_dir)
        self.classification_rules = self._load_classification_rules()

    def _load_classification_rules(self) -> Dict[str, Dict]:
        """加载分类规则"""
        return {
            # 角色创建类
            '人设生成': {
                'keywords': ['人设', '角色', '人物', '设定', '主角', '配角', '关系', '性格', '背景'],
                'path_patterns': ['人设生成', '人物卡'],
                'priority': 1
            },
            '续写正文': {
                'keywords': ['续写', '继续', '接下来', '正文', '故事', '情节'],
                'path_patterns': ['续写/续写正文', '正文续写'],
                'priority': 2
            },
            '续写章纲': {
                'keywords': ['章纲', '大纲', '章节', '细纲', '情节规划'],
                'path_patterns': ['续写/续写章纲', '章纲续写'],
                'priority': 3
            },
            '扩写润色': {
                'keywords': ['扩写', '润色', '优化', '完善', '丰富', '详细'],
                'path_patterns': ['扩写润色', '润色', '扩写'],
                'priority': 4
            },
            '降AI处理': {
                'keywords': ['降ai', '去ai', '人类', '伪装', '自然', '人工化'],
                'path_patterns': ['降ai', '去ai化'],
                'priority': 5
            },
            '脑洞生成': {
                'keywords': ['脑洞', '创意', '灵感', '点子', '想法'],
                'path_patterns': ['脑洞生成'],
                'priority': 6
            },
            '书名生成': {
                'keywords': ['书名', '标题', '作品名'],
                'path_patterns': ['书名生成'],
                'priority': 7
            },
            '开篇创作': {
                'keywords': ['开篇', '开头', '楔子', '黄金三章', '开局'],
                'path_patterns': ['开篇创作', '黄金开篇'],
                'priority': 8
            },
            '大纲生成': {
                'keywords': ['大纲', '细纲', '故事线', '结构', '框架'],
                'path_patterns': ['大纲生成', '细纲生成'],
                'priority': 9
            },
            '简介生成': {
                'keywords': ['简介', '介绍', '摘要', '概要'],
                'path_patterns': ['简介生成'],
                'priority': 10
            },
            '编辑建议': {
                'keywords': ['编辑', '建议', '修改', '校对', '审阅'],
                'path_patterns': ['编辑建议'],
                'priority': 11
            },
            '工具指令': {
                'keywords': ['工具', '实用', '功能', '指令'],
                'path_patterns': ['其他', '比较有用的'],
                'priority': 12
            }
        }

    def _classify_prompt(self, file_path: Path, content: str) -> Optional[int]:
        """智能分类提示词"""
        file_path_str = str(file_path).lower()

        # 计算每个分类的匹配分数
        category_scores = {}

        for category_name, rules in self.classification_rules.items():
            score = 0

            # 路径匹配（权重最高）
            for pattern in rules['path_patterns']:
                if pattern.lower() in file_path_str:
                    score += 10 * rules['priority']

            # 关键词匹配
            content_lower = content.lower()
            for keyword in rules['keywords']:
                if keyword.lower() in content_lower:
                    score += 5 * rules['priority']

                # 文件名匹配
                if keyword.lower() in file_path_str:
                    score += 3 * rules['priority']

            if score > 0:
                category_scores[category_name] = score

        # 返回分数最高的分类
        if category_scores:
            best_category = max(category_scores, key=category_scores.get)

            # 获取分类ID
            with get_db() as conn:
                category = get_category_by_name(conn, best_category)
                if category:
                    return category['id']

        return None  # 未分类

    def _extract_prompt_info(self, file_path: Path) -> Dict:
        """从文件提取提示词信息"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            if not content:
                return None

            # 提取标题（优先使用文件名）
            title = file_path.stem

            # 如果文件名太短或者只是数字，才使用内容第一行
            lines = content.split('\n')
            if len(title) < 3 or title.isdigit():
                title = lines[0].strip() if lines else file_path.stem
                # 清理标题（移除特殊符号）
                title = re.sub(r'^[#\-\*\s]+', '', title)

            # 限制标题长度
            if len(title) > 100:
                title = title[:97] + '...'

            # 提取描述（取前几行作为摘要）
            description_lines = []
            for i, line in enumerate(lines[1:6]):  # 取前5行
                line = line.strip()
                if line and not line.startswith('#') and not line.startswith('-'):
                    description_lines.append(line)

            description = ' '.join(description_lines) if description_lines else title
            if len(description) > 500:
                description = description[:497] + '...'

            # 智能分类
            category_id = self._classify_prompt(file_path, content)

            # 不提取标签，留空
            tags = []

            return {
                'name': title,
                'content': content,
                'description': description,
                'category_id': category_id,
                'tags': tags,
                'source': None,  # 不设置来源
                'file_path': file_path
            }

        except Exception as e:
            logger.error(f"读取文件失败 {file_path}: {e}")
            return None

    def _extract_tags(self, content: str, file_path: Path) -> List[str]:
        """提取标签"""
        tags = set()

        # 从文件路径提取标签
        path_parts = str(file_path).split('/')
        for part in path_parts:
            if part and part != file_path.name:
                tags.add(part.replace('.txt', '').replace(' ', ''))

        # 从内容中提取常见标签
        common_tags = [
            '小说', '写作', '创作', 'AI', '网文', '番茄', '起点',
            '主角', '角色', '情节', '故事', '大纲', '续写',
            '玄幻', '都市', '言情', '历史', '科幻', '游戏'
        ]

        content_lower = content.lower()
        for tag in common_tags:
            if tag.lower() in content_lower:
                tags.add(tag)

        return list(tags)[:10]  # 最多10个标签

    def _create_prompt(self, prompt_info: Dict) -> Optional[int]:
        """创建提示词"""
        try:
            with get_db() as conn:
                cur = conn.cursor()

                # 创建提示词记录
                cur.execute("""
                    INSERT INTO prompts(name, notes, category_id, created_at, updated_at)
                    VALUES(?, ?, ?, datetime('now'), datetime('now'))
                """, (
                    prompt_info['name'],
                    prompt_info['description'],
                    prompt_info['category_id']
                ))

                prompt_id = cur.lastrowid

                # 创建版本记录
                cur.execute("""
                    INSERT INTO versions(prompt_id, version, content, created_at)
                    VALUES(?, ?, ?, datetime('now'))
                """, (prompt_id, '1.0.0', prompt_info['content']))

                # 更新提示词的当前版本ID
                cur.execute("UPDATE prompts SET current_version_id = ? WHERE id = ?",
                           (cur.lastrowid, prompt_id))

                conn.commit()
                logger.info(f"成功导入提示词: {prompt_info['name']} (ID: {prompt_id})")
                return prompt_id

        except Exception as e:
            logger.error(f"创建提示词失败 {prompt_info['name']}: {e}")
            return None

    def scan_and_import(self) -> Dict:
        """扫描并导入所有提示词"""
        if not self.source_dir.exists():
            logger.error(f"源目录不存在: {self.source_dir}")
            return {'success': False, 'error': '源目录不存在'}

        # 初始化数据库
        init_database()

        # 扫描所有txt文件
        txt_files = list(self.source_dir.rglob('*.txt'))
        logger.info(f"发现 {len(txt_files)} 个提示词文件")

        results = {
            'total_files': len(txt_files),
            'imported': 0,
            'skipped': 0,
            'errors': 0,
            'categories': {},
            'details': []
        }

        for file_path in txt_files:
            try:
                prompt_info = self._extract_prompt_info(file_path)
                if not prompt_info:
                    results['skipped'] += 1
                    continue

                # 检查是否已存在（根据源路径）
                with get_db() as conn:
                    existing = conn.execute(
                        "SELECT id FROM prompts WHERE source = ?",
                        (prompt_info['source'],)
                    ).fetchone()

                    if existing:
                        logger.info(f"跳过已存在的提示词: {prompt_info['name']}")
                        results['skipped'] += 1
                        continue

                # 导入提示词
                prompt_id = self._create_prompt(prompt_info)
                if prompt_id:
                    results['imported'] += 1

                    # 统计分类
                    category_name = '未分类'
                    if prompt_info['category_id']:
                        with get_db() as conn:
                            category = get_category_by_id(conn, prompt_info['category_id'])
                            if category:
                                category_name = category['name']

                    results['categories'][category_name] = results['categories'].get(category_name, 0) + 1
                    results['details'].append({
                        'name': prompt_info['name'],
                        'category': category_name,
                        'id': prompt_id,
                        'file': str(file_path)
                    })
                else:
                    results['errors'] += 1

            except Exception as e:
                logger.error(f"处理文件失败 {file_path}: {e}")
                results['errors'] += 1

        return results

def main():
    """主函数"""
    if len(sys.argv) != 2:
        print("使用方法: python import_prompts.py <源目录>")
        sys.exit(1)

    source_dir = sys.argv[1]
    importer = PromptImporter(source_dir)

    logger.info(f"开始导入提示词从: {source_dir}")
    results = importer.scan_and_import()

    # 输出结果
    print("\n" + "="*50)
    print("导入结果统计:")
    print(f"总文件数: {results['total_files']}")
    print(f"成功导入: {results['imported']}")
    print(f"跳过文件: {results['skipped']}")
    print(f"错误文件: {results['errors']}")

    if results['categories']:
        print("\n分类统计:")
        for category, count in sorted(results['categories'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {category}: {count}")

    print("\n导入详情:")
    for detail in results['details'][:10]:  # 只显示前10个
        print(f"  {detail['name']} -> {detail['category']}")

    if len(results['details']) > 10:
        print(f"  ... 还有 {len(results['details']) - 10} 个提示词")

    print("="*50)

if __name__ == "__main__":
    main()