#!/usr/bin/env python3
"""
验证导入结果的脚本
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import get_db, init_database, get_all_categories

def verify_import():
    """验证导入结果"""
    # 初始化数据库
    init_database()

    with get_db() as conn:
        # 检查分类
        categories = get_all_categories(conn)
        print(f"分类总数: {len(categories)}")
        print("\n分类列表:")
        for cat in categories:
            parent = f" (父分类: {cat['parent_name']})" if cat['parent_name'] else ""
            print(f"  {cat['id']}: {cat['name']}{parent}")

        # 检查提示词
        prompts_count = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        print(f"\n提示词总数: {prompts_count}")

        # 按分类统计提示词
        category_stats = conn.execute("""
            SELECT c.name, COUNT(p.id) as count
            FROM categories c
            LEFT JOIN prompts p ON c.id = p.category_id
            GROUP BY c.id, c.name
            ORDER BY count DESC
        """).fetchall()

        print("\n分类提示词统计:")
        for stat in category_stats:
            print(f"  {stat['name']}: {stat['count']} 个")

        # 检查版本表
        versions_count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        print(f"\n版本记录总数: {versions_count}")

        # 显示前10个提示词
        prompts = conn.execute("""
            SELECT p.name, c.name as category_name, p.created_at
            FROM prompts p
            LEFT JOIN categories c ON p.category_id = c.id
            ORDER BY p.created_at DESC
            LIMIT 10
        """).fetchall()

        print("\n最新导入的提示词:")
        for i, prompt in enumerate(prompts, 1):
            category = prompt['category_name'] or "未分类"
            print(f"  {i}. {prompt['name']} -> {category}")

        # 检查是否有重复的源文件
        duplicates = conn.execute("""
            SELECT source, COUNT(*) as count
            FROM prompts
            WHERE source IS NOT NULL
            GROUP BY source
            HAVING count > 1
        """).fetchall()

        if duplicates:
            print(f"\n发现 {len(duplicates)} 个重复的源文件:")
            for dup in duplicates:
                print(f"  {dup['source']}: {dup['count']} 次")
        else:
            print("\n✅ 没有发现重复的源文件")

        print("\n✅ 导入验证完成!")

if __name__ == "__main__":
    verify_import()