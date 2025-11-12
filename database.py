"""
数据库模块 - 处理所有数据库相关操作
"""
import sqlite3
import json
import logging
from contextlib import contextmanager
from threading import Lock
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# 数据库路径
DB_PATH = os.environ.get('DB_PATH', 'D:\personalProject\prompt-manager\data\data.sqlite3')

class DatabasePool:
    """数据库连接池"""
    def __init__(self, max_connections=10):
        self.max_connections = max_connections
        self.connections = []
        self.lock = Lock()

    def get_connection(self):
        with self.lock:
            if self.connections:
                return self.connections.pop()
            else:
                return self._create_connection()

    def return_connection(self, conn):
        with self.lock:
            if len(self.connections) < self.max_connections:
                try:
                    conn.execute("SELECT 1")
                    self.connections.append(conn)
                except sqlite3.Error:
                    conn.close()

    def _create_connection(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 性能优化设置
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA temp_store=memory")
        return conn

# 全局数据库连接池
db_pool = DatabasePool()

@contextmanager
def get_db():
    """数据库连接上下文管理器"""
    conn = None
    try:
        conn = db_pool.get_connection()
        yield conn
    except Exception as e:
        logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            db_pool.return_connection(conn)

def init_database():
    """初始化数据库表和索引"""
    with get_db() as conn:
        cur = conn.cursor()

        # 创建提示词表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source TEXT,
                notes TEXT,
                color TEXT,
                tags TEXT,
                category_id INTEGER,
                pinned INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                current_version_id INTEGER,
                require_password INTEGER DEFAULT 0,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            )
        """)

        # 创建版本表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id INTEGER NOT NULL,
                version TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT,
                parent_version_id INTEGER,
                FOREIGN KEY(prompt_id) REFERENCES prompts(id)
            )
        """)

        # 创建设置表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # 创建AI配置表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'openai',
                model_name TEXT NOT NULL DEFAULT 'gpt-4',
                api_key TEXT,
                api_url TEXT,
                system_prompt TEXT DEFAULT '你是一个专业的提示词优化专家，请根据用户的要求优化提示词。',
                temperature REAL DEFAULT 0.7,
                max_tokens INTEGER DEFAULT 2000,
                created_at TEXT,
                updated_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)

        # 创建分类表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                color TEXT DEFAULT '#6B7280',
                icon TEXT DEFAULT 'folder',
                sort_order INTEGER DEFAULT 0,
                parent_id INTEGER,
                created_at TEXT,
                FOREIGN KEY(parent_id) REFERENCES categories(id)
            )
        """)

        # 创建优化任务表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS optimization_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id INTEGER NOT NULL,
                ai_config_id INTEGER,
                original_content TEXT,
                optimized_content TEXT,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT,
                completed_at TEXT,
                FOREIGN KEY(prompt_id) REFERENCES prompts(id),
                FOREIGN KEY(ai_config_id) REFERENCES ai_configs(id)
            )
        """)

        # 创建性能优化索引
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_name ON prompts(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_created_at ON prompts(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_updated_at ON prompts(updated_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_pinned ON prompts(pinned)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_id ON versions(prompt_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_created_at ON versions(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_version ON versions(version)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_configs_active ON ai_configs(is_active)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_optimization_tasks_status ON optimization_tasks(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_name ON categories(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent_id ON categories(parent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_sort_order ON categories(sort_order)")

        # 插入默认设置
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('version_cleanup_threshold', '200')")
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_mode', 'off')")
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_password_hash', '')")
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('language', 'zh')")

        # 插入默认AI配置
        cur.execute("INSERT OR IGNORE INTO ai_configs(id, name, provider, model_name, created_at, updated_at) VALUES(1, '默认配置', 'openai', 'gpt-4', datetime('now'), datetime('now'))")

        # 数据库迁移：添加category_id字段（如果不存在）
        try:
            cur.execute("ALTER TABLE prompts ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        except sqlite3.OperationalError:
            pass  # 字段已存在

        # 插入默认分类
        default_categories = [
            (1, '角色创建', '角色生成、人物设定、关系构建', '#EF4444', 'users', 0, None),
            (2, '内容创作', '续写、扩写、润色、大纲生成', '#3B82F6', 'edit', 1, None),
            (3, '编辑优化', '编辑建议、降AI处理、风格调整', '#10B981', 'check-circle', 2, None),
            (4, '创意生成', '脑洞生成、书名、简介、开篇创作', '#F59E0B', 'lightbulb', 3, None),
            (5, '工具指令', '实用工具、其他功能指令', '#6B7280', 'tool', 4, None),
            (6, '续写正文', '小说正文续写相关指令', '#3B82F6', 'file-text', 10, 2),
            (7, '续写章纲', '章节大纲续写指令', '#3B82F6', 'list', 11, 2),
            (8, '扩写润色', '内容扩展和润色优化', '#10B981', 'refresh', 20, 3),
            (9, '降AI处理', '去除AI痕迹，人工化处理', '#10B981', 'human', 21, 3),
            (10, '人设生成', '人物角色生成和设定', '#EF4444', 'user-plus', 30, 1),
            (11, '脑洞生成', '创意点子和灵感生成', '#F59E0B', 'sparkles', 40, 4),
            (12, '书名生成', '小说标题创作指令', '#F59E0B', 'type', 41, 4),
            (13, '开篇创作', '小说开头和黄金开篇', '#F59E0B', 'book-open', 42, 4),
            (14, '大纲生成', '故事大纲和细纲生成', '#3B82F6', 'sitemap', 50, 2),
            (15, '简介生成', '作品简介和内容摘要', '#F59E0B', 'file-text', 51, 4),
        ]

        for cat in default_categories:
            cur.execute("""
                INSERT OR IGNORE INTO categories(id, name, description, color, icon, sort_order, parent_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, cat)

        conn.commit()
        logger.info("Database initialized successfully")

def get_setting(conn, key, default=None):
    """获取设置值"""
    result = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return result['value'] if result else default

def set_setting(conn, key, value):
    """设置值"""
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))

def now_ts():
    """获取当前时间戳"""
    return datetime.utcnow().isoformat()

def parse_tags(tags_string):
    """解析标签字符串为列表"""
    if not tags_string:
        return []
    try:
        return json.loads(tags_string)
    except:
        return [tag.strip() for tag in tags_string.split(',') if tag.strip()]

def tags_to_text(tags):
    """将标签列表转换为文本"""
    if not tags:
        return ""
    try:
        return json.dumps(tags, ensure_ascii=False)
    except:
        return ", ".join(tags)

def bump_version(current, kind='patch'):
    """版本号升级"""
    if not current:
        return "1.0.0"

    try:
        parts = current.split('.')
        if len(parts) != 3:
            return "1.0.0"

        major, minor, patch = map(int, parts)

        if kind == 'major':
            major += 1
            minor = 0
            patch = 0
        elif kind == 'minor':
            minor += 1
            patch = 0
        else:  # patch
            patch += 1

        return f"{major}.{minor}.{patch}"
    except:
        return "1.0.0"

# 分类相关函数
def get_all_categories(conn):
    """获取所有分类"""
    return conn.execute("""
        SELECT c.*, p.name as parent_name
        FROM categories c
        LEFT JOIN categories p ON c.parent_id = p.id
        ORDER BY c.sort_order, c.name
    """).fetchall()

def get_category_by_id(conn, category_id):
    """根据ID获取分类"""
    return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()

def get_category_by_name(conn, name):
    """根据名称获取分类"""
    return conn.execute("SELECT * FROM categories WHERE name = ?", (name,)).fetchone()

def create_category(conn, name, description=None, color=None, icon=None, parent_id=None):
    """创建分类"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO categories(name, description, color, icon, parent_id, created_at)
        VALUES(?, ?, ?, ?, ?, datetime('now'))
    """, (name, description, color, icon, parent_id))
    conn.commit()
    return cur.lastrowid

def update_category(conn, category_id, **kwargs):
    """更新分类"""
    fields = []
    values = []
    for field in ['name', 'description', 'color', 'icon', 'parent_id']:
        if field in kwargs and kwargs[field] is not None:
            fields.append(f"{field} = ?")
            values.append(kwargs[field])

    if fields:
        values.append(category_id)
        conn.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()

def delete_category(conn, category_id):
    """删除分类（需要先检查是否有提示词使用）"""
    # 检查是否有提示词使用此分类
    prompts_count = conn.execute("SELECT COUNT(*) FROM prompts WHERE category_id = ?", (category_id,)).fetchone()[0]
    if prompts_count > 0:
        raise ValueError(f"无法删除分类，还有 {prompts_count} 个提示词使用此分类")

    # 检查是否有子分类
    children_count = conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id = ?", (category_id,)).fetchone()[0]
    if children_count > 0:
        raise ValueError("无法删除分类，还有子分类")

    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()

def get_prompts_by_category(conn, category_id):
    """获取指定分类的提示词"""
    return conn.execute("""
        SELECT p.*, c.name as category_name
        FROM prompts p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.category_id = ?
        ORDER BY p.created_at DESC
    """, (category_id,)).fetchall()