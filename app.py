import json
import os
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from io import BytesIO
import re
import requests
import hashlib

# 导入自定义模块
from database import (
    get_db, init_database, get_setting, set_setting,
    now_ts, parse_tags, tags_to_text, bump_version, DB_PATH
)
from validators import (
    validate_prompt_name, validate_prompt_content, validate_tags,
    validate_color, validate_version, validate_source, validate_notes,
    sanitize_search_query, validate_pagination_params
)
from decorators import (
    handle_database_errors, handle_validation_errors,
    log_api_calls, require_auth, validate_json_content_type
)
from ai_service import create_ai_service, AIServiceError, estimate_cost
from jasypt_utils import JasyptEncryptor

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source TEXT,
                notes TEXT,
                color TEXT,
                tags TEXT,
                pinned INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                current_version_id INTEGER,
                require_password INTEGER DEFAULT 0
            )
            """
        )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            parent_version_id INTEGER,
            FOREIGN KEY(prompt_id) REFERENCES prompts(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # 创建性能优化索引
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_name ON prompts(name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_created_at ON prompts(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_updated_at ON prompts(updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_pinned ON prompts(pinned)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_id ON versions(prompt_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_created_at ON versions(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_versions_version ON versions(version)")
    # 默认阈值 200
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('version_cleanup_threshold', '200')")
    # 简易认证默认设置
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_mode', 'off')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_password_hash', '')")
    # 全局语言设置，默认中文
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('language', 'zh')")

    # AI 配置表
    cur.execute(
        """
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
        """
    )

    # 提示词优化任务表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS optimization_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_prompt_id INTEGER NOT NULL,
            source_version_id INTEGER NOT NULL,
            ai_config_id INTEGER NOT NULL,
            optimization_prompt TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result_content TEXT,
            error_message TEXT,
            created_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(source_prompt_id) REFERENCES prompts(id),
            FOREIGN KEY(ai_config_id) REFERENCES ai_configs(id)
        )
        """
    )

    # 版本关联表 (记录优化关系)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS version_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_version_id INTEGER NOT NULL,
            optimized_version_id INTEGER NOT NULL,
            optimization_task_id INTEGER NOT NULL,
            improvement_score REAL,
            created_at TEXT,
            FOREIGN KEY(optimized_version_id) REFERENCES versions(id),
            FOREIGN KEY(original_version_id) REFERENCES versions(id),
            FOREIGN KEY(optimization_task_id) REFERENCES optimization_tasks(id)
        )
        """
    )

    # 默认 AI 配置
    cur.execute(
        "INSERT OR IGNORE INTO ai_configs(id, name, provider, model_name, created_at, updated_at) VALUES(1, '默认配置', 'openai', 'gpt-4', datetime('now'), datetime('now'))")

    conn.commit()


def now_ts():
    return datetime.utcnow().isoformat()


def parse_tags(s):
    if not s:
        return []
    if isinstance(s, list):
        return s
    # 输入支持中文逗号/英文逗号/空格；保留层级如“场景/客服”
    parts = []
    for raw in s.replace('，', ',').split(','):
        p = raw.strip()
        if p:
            parts.append(p)
    return parts


def tags_to_text(tags):
    return ', '.join(tags)


def get_setting(conn, key, default=None):
    if hasattr(conn, 'execute'):
        # 传入的是数据库连接对象
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default
    else:
        # 传入的是上下文管理器或其他对象，使用新的数据库连接
        with get_db() as db_conn:
            row = db_conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row['value'] if row else default


def set_setting(conn, key, value):
    if hasattr(conn, 'execute'):
        # 传入的是数据库连接对象
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        conn.commit()
    else:
        # 传入的是上下文管理器或其他对象，使用新的数据库连接
        with get_db() as db_conn:
            db_conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            db_conn.commit()


def bump_version(current, kind='patch'):
    if not current:
        return '1.0.0'
    try:
        major, minor, patch = [int(x) for x in current.split('.')]
    except Exception:
        # 容错：无法解析直接回到 1.0.0
        return '1.0.0'
    if kind == 'major':
        major += 1
        minor = 0
        patch = 0
    elif kind == 'minor':
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def prune_versions(conn, prompt_id):
    threshold_s = get_setting(conn, 'version_cleanup_threshold', '200')
    try:
        threshold = int(threshold_s)
    except Exception:
        threshold = 200
    rows = conn.execute(
        "SELECT id FROM versions WHERE prompt_id=? ORDER BY created_at DESC", (prompt_id,)
    ).fetchall()
    if len(rows) > threshold:
        to_delete = [r['id'] for r in rows[threshold:]]
        conn.executemany("DELETE FROM versions WHERE id=?", [(vid,) for vid in to_delete])


def compute_current_version(conn, prompt_id):
    row = conn.execute(
        "SELECT id FROM versions WHERE prompt_id=? ORDER BY created_at DESC LIMIT 1",
        (prompt_id,),
    ).fetchone()
    if row:
        conn.execute("UPDATE prompts SET current_version_id=?, updated_at=? WHERE id=?",
                     (row['id'], now_ts(), prompt_id))


def get_all_tags(conn):
    all_rows = conn.execute("SELECT tags FROM prompts WHERE tags IS NOT NULL AND tags != ''").fetchall()
    tags = set()
    for r in all_rows:
        try:
            arr = json.loads(r['tags'])
            for t in arr:
                tags.add(t)
        except Exception:
            pass
    return sorted(tags)


def ensure_db():
    # Ensure parent directory exists to avoid 'unable to open database file'
    try:
        os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    except Exception:
        # best-effort; continue to let sqlite raise helpful error if needed
        pass
    if not os.path.exists(DB_PATH):
        init_db()
    else:
        # best-effort migrations for new versions
        migrate_schema()


def migrate_schema():
    """Run lightweight schema migrations to add new columns/settings if missing."""
    try:
        with get_db() as conn:
            # ensure prompts.require_password exists
            cols = [r['name'] for r in conn.execute('PRAGMA table_info(prompts)').fetchall()]
            if 'require_password' not in cols:
                conn.execute("ALTER TABLE prompts ADD COLUMN require_password INTEGER DEFAULT 0")
            # ensure prompts.color exists
            cols = [r['name'] for r in conn.execute('PRAGMA table_info(prompts)').fetchall()]
            if 'color' not in cols:
                conn.execute("ALTER TABLE prompts ADD COLUMN color TEXT")

            # Create AI-related tables if they don't exist
            existing_tables = [row['name'] for row in
                               conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

            if 'ai_configs' not in existing_tables:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS ai_configs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT 'openai',
                        model_name TEXT NOT NULL DEFAULT 'gpt-4',
                        api_key TEXT,
                        api_url TEXT,
                        system_prompt TEXT DEFAULT '你是一个专业的提示词优化专家。',
                        temperature REAL DEFAULT 0.7,
                        max_tokens INTEGER DEFAULT 2000,
                        is_active INTEGER DEFAULT 1,
                        created_at INTEGER DEFAULT (strftime('%s', 'now')),
                        updated_at INTEGER DEFAULT (strftime('%s', 'now'))
                    )
                ''')

            if 'optimization_tasks' not in existing_tables:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS optimization_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        prompt_id INTEGER NOT NULL,
                        ai_config_id INTEGER NOT NULL,
                        original_version_id INTEGER,
                        optimization_prompt TEXT,
                        status TEXT DEFAULT 'pending',
                        error_message TEXT,
                        result_content TEXT,
                        new_version_id INTEGER,
                        estimated_cost REAL,
                        actual_cost REAL,
                        created_at INTEGER DEFAULT (strftime('%s', 'now')),
                        updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                        FOREIGN KEY (prompt_id) REFERENCES prompts (id),
                        FOREIGN KEY (ai_config_id) REFERENCES ai_configs (id)
                    )
                ''')

            if 'version_relations' not in existing_tables:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS version_relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL,
                        original_version_id INTEGER,
                        optimized_version_id INTEGER,
                        relation_type TEXT DEFAULT 'optimization',
                        created_at INTEGER DEFAULT (strftime('%s', 'now')),
                        FOREIGN KEY (task_id) REFERENCES optimization_tasks (id)
                    )
                ''')

            # ensure auth settings keys exist
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_mode', 'off')")
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auth_password_hash', '')")
            # ensure language setting exists
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('language', 'zh')")
            conn.commit()
    except Exception as e:
        # ignore migration failures to avoid blocking the app
        logger.error(f"Database migration failed: {e}")
        pass


app = Flask(__name__)
# Respect X-Forwarded-* headers when behind reverse proxies (e.g., Nginx)
# This ensures request.url/request.host reflect the external scheme/host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')
# Jinja 过滤器：JSON 反序列化
app.jinja_env.filters['loads'] = json.loads

# === 简易国际化（无第三方依赖） ===
# 通过 settings 表中的 key=language 控制全局语言，默认 zh。
# 在模板中使用 {{ t('中文文案') }} 进行翻译；未命中时回退原文。
LANG_DEFAULT = 'zh'
TRANSLATIONS = {
    'en': {
        # 通用 / 导航
        '提示词管理': 'Prompt Manager',
        '设置': 'Settings',
        '切换主题': 'Toggle Theme',
        '返回': 'Back',
        '取消': 'Cancel',
        '保存': 'Save',
        'Prompt 管理器': 'Prompt Manager',
        '列表': 'List',
        '详情': 'Details',
        '首页': 'Home',

        # 设置页
        '系统设置': 'System Settings',
        '管理您的提示词库配置': 'Manage your prompt library configuration',
        '版本历史清理': 'Version History Cleanup',
        '每个提示词仅保留最近 N 个版本，超出将自动清理（默认 200）。': 'Keep only the latest N versions per prompt. Older versions beyond this limit are auto-pruned (default 200).',
        '清理阈值 N': 'Cleanup threshold N',
        '个版本': 'versions',
        '访问密码': 'Access Password',
        '三选一：关闭（不需要密码）、指定提示词密码（仅对勾选了“需要密码”的提示词生效）、全局密码（访问本站任意页面需要密码）。': 'Choose one: Off (no password), Per-prompt password (only for prompts marked "Require password"), or Global password (require password for any page).',
        '密码模式': 'Password mode',
        '关闭': 'Off',
        '指定提示词密码': 'Per-prompt password',
        '全局密码': 'Global password',
        '设置/修改密码（4-8 位）': 'Set/Change password (4–8 digits)',
        '当前密码（已设置时必填）': 'Current password (required if already set)',
        '新密码（留空则不修改）': 'New password (leave empty to keep)',
        '确认新密码': 'Confirm new password',
        '已设置密码：修改密码或切换密码模式需先验证当前密码。': 'Password set: verify current password before changing it or switching modes.',
        '如从未设置过密码，请先设置后再开启对应模式。': 'If no password was set, set one first before enabling a mode.',
        '数据导入 / 导出': 'Import / Export',
        '导出数据': 'Export data',
        '将所有提示词和版本历史导出为 JSON 格式文件': 'Export all prompts and version history as a JSON file',
        '导出全部数据': 'Export all data',
        '导入数据': 'Import data',
        '导入将覆盖所有现有数据，请谨慎操作': 'Import will overwrite all existing data. Proceed with caution.',
        '选择 JSON 文件': 'Choose JSON file',
        '保存设置 / 执行导入': 'Save settings / Run import',

        # 语言设置
        '语言': 'Language',
        '系统语言': 'System language',
        '中文': 'Chinese',
        '英文': 'English',

        # Flash/消息
        '已保存': 'Saved',
        '未找到该提示词': 'Prompt not found',
        '已创建提示词并保存首个版本': 'Prompt created and first version saved',
        '提示词不存在或已被删除': 'Prompt does not exist or has been deleted',
        '已删除提示词及其所有版本': 'Prompt and all versions deleted',
        '删除失败，请重试': 'Deletion failed, please try again',
        '版本不存在': 'Version not found',
        '已从历史版本回滚并创建新版本': 'Rolled back from history and created a new version',
        '阈值需为正整数': 'Threshold must be a positive integer',
        '设置已保存': 'Settings saved',
        '请先输入当前密码以修改认证设置': 'Enter current password to modify authentication settings',
        '当前密码不正确，无法修改认证设置': 'Incorrect current password, cannot modify authentication settings',
        '请先设置访问密码（4-8 位）': 'Please set an access password (4–8 digits) first',
        '两次输入的密码不一致': 'Passwords do not match',
        '密码长度需为 4-8 位': 'Password length must be 4–8 digits',
        '已导入并覆盖所有数据': 'Imported and overwrote all data',
        '导入失败：上传表单解析错误': 'Import failed: invalid upload form data',
        '导入失败：JSON 格式无效': 'Import failed: invalid JSON',
        '导入失败，请重试': 'Import failed, please try again',
        '暂无版本': 'No versions yet',
        '所选版本不存在': 'Selected version does not exist',
        '已通过认证': 'Authenticated',
        '密码不正确': 'Incorrect password',
        '已退出登录': 'Logged out',
        '已解锁该提示词': 'Prompt unlocked',

        # 首页 index
        '搜索（名称/来源/备注/标签/当前内容）': 'Search (name/source/notes/tags/content)',
        '排序': 'Sort',
        '最近修改': 'Recently updated',
        '创建时间': 'Created time',
        '名称 A-Z': 'Name A–Z',
        '标签': 'Tags',
        '应用': 'Apply',
        '新建提示词': 'New Prompt',
        '展开/收起筛选': 'Toggle filters',
        '筛选侧边栏': 'Filter sidebar',
        '筛选': 'Filters',
        '收起筛选': 'Collapse filters',
        '全部': 'All',
        '暂无标签': 'No tags',
        '来源': 'Source',
        '未设置': 'Not set',
        '暂无来源': 'No sources',
        '没有符合筛选条件的结果': 'No results match the filters',
        '调整或清空筛选条件后再试试': 'Try adjusting or clearing filters',
        '清空筛选条件': 'Clear filters',
        '暂无提示词': 'No prompts yet',
        '点击"新建提示词"开始创建您的第一个提示词': 'Click "New Prompt" to create your first one',
        '创建第一个提示词': 'Create first prompt',
        '总计': 'Total',
        '置顶': 'Pinned',
        '切换布局': 'Toggle view',
        '置顶/取消置顶': 'Pin/Unpin',
        '来源：': 'Source: ',
        '需要密码': 'Password required',
        '修改：': 'Updated: ',
        '版本：': 'Version: ',
        '该提示词受密码保护': 'This prompt is password-protected',
        '内容预览': 'Preview',
        '复制预览内容': 'Copy preview',

        # 详情/编辑 prompt_detail
        '提示词编辑': 'Edit Prompt',
        '返回列表': 'Back to list',
        '历史版本': 'Versions',
        '基本信息': 'Basic Info',
        '提示词名称': 'Prompt name',
        '输入提示词的名称': 'Enter prompt name',
        '提示词内容': 'Prompt content',
        '在此输入提示词的完整内容...': 'Enter full prompt content here...',
        '字符': 'chars',
        '复制内容': 'Copy content',
        '自动调整大小': 'Auto-resize',
        '清空内容': 'Clear content',
        '高级设置': 'Advanced Settings',
        '提示词来源': 'Prompt source',
        '标签，用逗号分隔': 'Tags, separated by commas',
        '颜色': 'Color',
        '选择颜色': 'Pick color',
        '例如 #409eff，留空不设置': 'e.g. #409eff, leave empty to unset',
        '清除颜色': 'Clear color',
        '用于首页卡片边框的细微彩色外圈。留空则不设置。': 'Used for a subtle colored ring on the home card border. Leave empty to skip.',
        '备注': 'Notes',
        '补充说明或使用注意事项': 'Additional notes or usage tips',
        '该提示词需要密码访问': 'This prompt requires a password',
        '已开启全局密码，单个提示词的密码设置不再生效。': 'Global password is enabled; per-prompt password no longer applies.',
        '当前未启用“指定提示词密码”模式，本项暂不生效。': 'Per-prompt password mode is not enabled; this setting is inactive.',
        '保存修改': 'Save changes',
        '创建提示词': 'Create prompt',
        '删除提示词': 'Delete prompt',
        '保存为新版本': 'Save as new version',
        '补丁版本 (+0.0.1)': 'Patch (+0.0.1)',
        '次版本 (+0.1.0)': 'Minor (+0.1.0)',
        '主版本 (+1.0.0)': 'Major (+1.0.0)',
        '提示词预览': 'Prompt preview',
        '保存中...': 'Saving...',
        '确定要删除该提示词及其所有版本吗？此操作不可恢复。': 'Delete this prompt and all versions? This cannot be undone.',
        '请输入提示词名称': 'Please enter a prompt name',
        '请输入提示词内容': 'Please enter prompt content',
        '未命名提示词': 'Untitled prompt',
        '无内容': 'No content',
        '已开启自动调整大小': 'Auto-resize enabled',
        '没有内容可复制': 'No content to copy',
        '复制失败，请手动选择文本复制': 'Copy failed, please select text manually',
        '确定要清空内容吗？此操作不可撤销。': 'Clear content? This cannot be undone.',

        # 历史版本 versions
        '历史版本 -': 'Version History -',
        '创建于': 'Created at',
        '暂无历史版本': 'No version history',
        '该提示词还没有保存过任何版本历史。': 'This prompt has no saved version history yet.',
        '开始编辑并保存版本来追踪内容变化。': 'Start editing and saving versions to track changes.',
        '返回首页': 'Back to Home',
        '总版本数': 'Total versions',
        '最近更新': 'Last updated',
        '当前版本': 'Current version',
        '选择版本对比': 'Choose versions to compare',
        '版本历史': 'Version history',
        '按时间倒序排列，最新的版本显示在最前面': 'Ordered by time (newest first)',
        '查看完整版本内容': 'View full version content',
        '查看详情': 'View details',
        '与当前版本对比': 'Compare with current',
        '对比差异': 'Compare differences',
        '基于此版本内容创建新版本': 'Create a new version based on this content',
        '恢复到此版本': 'Roll back to this version',
        '当前使用中': 'In use',
        '版本内容': 'Version content',
        '复制': 'Copy',
        '选择对比版本': 'Choose versions to compare',
        '左侧版本：': 'Left version: ',
        '右侧版本：': 'Right version: ',
        '开始对比': 'Compare',
        '版本': 'Version',
        '版本信息不存在，请刷新页面重试': 'Version not found, please refresh and retry',
        '页面加载错误，请刷新页面重试': 'Page load error, please refresh and retry',
        '请选择要对比的版本': 'Please select versions to compare',
        '请选择两个不同的版本进行对比': 'Please select two different versions',
        '未知': 'Unknown',
        '确定要回滚到版本 {version} 吗？': 'Confirm rollback to version {version}?',
        '📝 回滚说明：': 'Notes:',
        '• 这将基于版本 {version} 的内容创建一个新版本': '• A new version will be created based on version {version}\'s content',
        '• 当前版本 {current} 不会被删除': '• Current version {current} will not be deleted',
        '• 新版本号将在当前版本基础上递增': '• The new version number will be incremented from current version',
        '• 所有版本历史都会保留': '• All version history will be kept',
        '此操作不可撤销，是否继续？': 'This action cannot be undone. Continue?',
        '操作失败，请刷新页面重试': 'Operation failed, please refresh and retry',

        # Diff 页面
        '版本对比': 'Compare Versions',
        '返回编辑': 'Back to edit',
        '左（旧）': 'Left (old)',
        '右（新）': 'Right (new)',
        '模式': 'Mode',
        '词级': 'Word-level',
        '行级': 'Line-level',
        '刷新': 'Refresh',
        '旧版本：': 'Old: ',
        '新版本：': 'New: ',

        # Auth 页面
        '安全验证': 'Security Check',
        '访问验证': 'Access Verification',
        '解锁提示词': 'Unlock Prompt',
        '请输入访问密码以进入站点': 'Enter password to access the site',
        '该提示词已启用密码保护，请输入密码解锁': 'This prompt is password-protected; enter password to unlock',
        '提示词': 'Prompt',
        '访问密码（4-8 位）': 'Access password (4–8 digits)',
        '请输入密码': 'Enter password',
        '进入': 'Enter',
        '解锁': 'Unlock',
    }
}


def _get_language():
    """读取全局语言设置（zh|en），默认 zh。"""
    try:
        lang = get_setting(None, 'language', LANG_DEFAULT) or LANG_DEFAULT
        return 'en' if lang.lower() == 'en' else 'zh'
    except Exception:
        return LANG_DEFAULT


@app.context_processor
def inject_i18n():
    lang = _get_language()

    def t(s: object) -> str:
        text = '' if s is None else str(s)
        if lang == 'en':
            return TRANSLATIONS.get('en', {}).get(text, text)
        return text

    return {
        't': t,
        'lang': lang,
        'lang_html': 'en' if lang == 'en' else 'zh-CN',
    }


def sanitize_color(val):
    """Normalize color to #RRGGBB or return None if invalid/empty.
    Accepts #RGB or #RRGGBB (case-insensitive). Returns lowercase #rrggbb.
    """
    s = (val or '').strip()
    if not s:
        return None
    if re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s):
        if len(s) == 4:
            # expand #RGB to #RRGGBB
            s = '#' + ''.join([c * 2 for c in s[1:]])
        return s.lower()
    return None


@app.before_request
def _before():
    ensure_db()
    # 全局密码模式拦截：除登录与静态资源外均需认证
    try:
        mode = get_setting(None, 'auth_mode', 'off') or 'off'
    except Exception:
        mode = 'off'
    if mode == 'global':
        # Allow login and static assets without auth
        allowed = (request.endpoint in {'login', 'static'}) or request.path.startswith('/static/')
        if not allowed and not session.get('auth_ok'):
            # 使用相对路径避免因反向代理造成的主机/协议不一致
            # 例如浏览器在 https 域名访问，但后端看到的是 http://127.0.0.1
            # 这里将 next 归一化为相对路径，既安全也能避免跳回 127.0.0.1
            nxt = request.full_path if request.query_string else request.path
            nxt = nxt.rstrip('?')  # 某些情况下 full_path 末尾会带一个多余的 ?
            return redirect(url_for('login', next=nxt))


@app.route('/')
@handle_database_errors
def index():
    """优化的首页函数 - 提升查询性能"""
    with get_db() as conn:
        auth_mode = get_setting(conn, 'auth_mode', 'off') or 'off'

        # 获取查询参数
        q = request.args.get('q', '').strip()
        sort = request.args.get('sort', 'updated')

        # 处理标签筛选
        selected_tags = [t.strip() for t in request.args.getlist('tag') if t.strip()]
        if not selected_tags and request.args.get('tags'):
            selected_tags = [t.strip() for t in request.args.get('tags', '').replace('，', ',').split(',') if t.strip()]

        # 处理来源筛选
        selected_sources = [s.strip() for s in request.args.getlist('source') if s.strip()]
        if not selected_sources and request.args.get('sources'):
            selected_sources = [s.strip() for s in request.args.get('sources', '').replace('，', ',').split(',') if
                                s.strip()]

        # 构建排序
        order_clause = 'pinned DESC,'
        if sort == 'created':
            order_clause += ' p.created_at DESC, p.id DESC'
        elif sort == 'name':
            order_clause += ' p.name COLLATE NOCASE ASC'
        elif sort == 'tags':
            order_clause += ' p.tags COLLATE NOCASE ASC'
        else:
            order_clause += ' p.updated_at DESC, p.id DESC'

        # 构建优化的SQL查询
        sql_conditions = []
        sql_params = []

        # 搜索条件
        if q:
            like = f"%{q}%"
            sql_conditions.append(
                "(p.name LIKE ? OR p.source LIKE ? OR p.notes LIKE ? OR p.tags LIKE ? OR v.content LIKE ?)")
            sql_params.extend([like, like, like, like, like])

        # 标签筛选条件 - 使用JSON函数优化
        if selected_tags:
            tag_conditions = []
            for tag in selected_tags:
                tag_conditions.append("p.tags LIKE ?")
                sql_params.append(f"%{tag}%")
            sql_conditions.append(f"({' OR '.join(tag_conditions)})")

        # 来源筛选条件
        if selected_sources:
            source_conditions = []
            for source in selected_sources:
                if source == '(empty)':
                    source_conditions.append("(p.source IS NULL OR p.source = '')")
                else:
                    source_conditions.append("p.source = ?")
                    sql_params.append(source)
            sql_conditions.append(f"({' OR '.join(source_conditions)})")

        # 构建完整SQL
        sql = f"""
            SELECT p.*, v.content as current_content, v.version as current_version
            FROM prompts p
            LEFT JOIN versions v ON v.id = p.current_version_id
        """

        if sql_conditions:
            sql += " WHERE " + " AND ".join(sql_conditions)

        sql += f" ORDER BY {order_clause}"

        # 执行查询
        prompts = conn.execute(sql, sql_params).fetchall()

        # 处理访问权限
        unlocked = set(session.get('unlocked_prompts') or [])
        locked_ids = set()

        if auth_mode == 'per':
            for r in prompts:
                if r['require_password'] and (r['id'] not in unlocked):
                    locked_ids.add(r['id'])

        # 应用额外的筛选（针对复杂的JSON标签查询）
        if selected_tags:
            def has_selected_tag(row_tags_json, selected_tags):
                try:
                    row_tags = json.loads(row_tags_json) if row_tags_json else []
                    return any(tag in row_tags for tag in selected_tags)
                except:
                    return False

            prompts = [r for r in prompts if has_selected_tag(r['tags'], selected_tags)]

        # 统计信息（使用优化的聚合查询）
        tag_counts = {}
        source_counts = {}

        def norm_source(s):
            return (s or '').strip() or '(empty)'

        # 只对可见的项目进行统计
        visible_prompts = [r for r in prompts if not (auth_mode == 'per' and r['id'] in locked_ids)]

        for r in visible_prompts:
            try:
                tags = json.loads(r['tags']) if r['tags'] else []
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

                source = norm_source(r['source'])
                source_counts[source] = source_counts.get(source, 0) + 1
            except:
                continue

        # 获取标签建议（优化查询）
        tag_suggestions = []
        if auth_mode == 'per':
            # 在密码保护模式下，只获取已解锁的标签
            unlocked_ids_str = ','.join(str(id) for id in unlocked) if unlocked else '0'
            tag_sql = f"""
                SELECT DISTINCT tags FROM prompts
                WHERE (require_password = 0 OR id IN ({unlocked_ids_str}))
                AND tags IS NOT NULL AND tags != ''
            """
        else:
            tag_sql = "SELECT DISTINCT tags FROM prompts WHERE tags IS NOT NULL AND tags != ''"

        tag_rows = conn.execute(tag_sql).fetchall()
        for r in tag_rows:
            try:
                tags = json.loads(r['tags']) if r['tags'] else []
                for tag in tags:
                    if tag not in tag_suggestions:
                        tag_suggestions.append(tag)
            except:
                continue
    return render_template(
        'index.html',
        prompts=prompts,
        q=q,
        sort=sort,
        tag_suggestions=tag_suggestions,
        tag_counts=tag_counts,
        source_counts=source_counts,
        selected_tags=selected_tags,
        selected_sources=selected_sources,
        auth_mode=auth_mode,
        locked_ids=list(locked_ids),
    )


@app.route('/prompt/new', methods=['GET', 'POST'])
def new_prompt():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or '未命名提示词'
        source = request.form.get('source', '').strip()
        notes = request.form.get('notes', '').strip()
        color = sanitize_color(request.form.get('color'))
        tags = parse_tags(request.form.get('tags', ''))
        content = request.form.get('content', '')
        bump_kind = request.form.get('bump_kind', 'patch')
        require_password = 1 if request.form.get('require_password') == '1' else 0

        with get_db() as conn:
            cur = conn.cursor()
            ts = now_ts()
            cur.execute(
                "INSERT INTO prompts(name, source, notes, color, tags, pinned, created_at, updated_at, require_password) VALUES(?,?,?,?,?,0,?,?,?)",
                (name, source, notes, color, json.dumps(tags, ensure_ascii=False), ts, ts, require_password)
            )
            pid = cur.lastrowid
            version = bump_version(None, bump_kind)
            cur.execute(
                "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,NULL)",
                (pid, version, content, ts)
            )
            vid = cur.lastrowid
            cur.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (vid, pid))
            prune_versions(conn, pid)
        flash('已创建提示词并保存首个版本', 'success')
        return redirect(url_for('prompt_detail', prompt_id=pid))
    # 读取认证模式控制复选框可用性
    auth_mode = get_setting(None, 'auth_mode', 'off') or 'off'
    return render_template('prompt_detail.html', prompt=None, versions=[], current=None, auth_mode=auth_mode)


@app.route('/prompt/<int:prompt_id>', methods=['GET', 'POST'])
def prompt_detail(prompt_id):
    with get_db() as conn:
        auth_mode = get_setting(conn, 'auth_mode', 'off') or 'off'

        if request.method == 'POST':
            # 保存新版本或仅更新元信息
            name = request.form.get('name', '').strip() or '未命名提示词'
            source = request.form.get('source', '').strip()
            notes = request.form.get('notes', '').strip()
            color = sanitize_color(request.form.get('color'))
            tags = parse_tags(request.form.get('tags', ''))
            content = request.form.get('content', '')
            bump_kind = request.form.get('bump_kind', 'patch')
            do_save_version = request.form.get('do_save_version') == '1'
            require_password = 1 if request.form.get('require_password') == '1' else 0
            ts = now_ts()

            conn.execute(
                "UPDATE prompts SET name=?, source=?, notes=?, color=?, tags=?, updated_at=?, require_password=? WHERE id=?",
                (name, source, notes, color, json.dumps(tags, ensure_ascii=False), ts, require_password, prompt_id))

            if do_save_version:
                # 取当前版本号
                row = conn.execute(
                    "SELECT v.version FROM prompts p LEFT JOIN versions v ON v.id=p.current_version_id WHERE p.id=?",
                    (prompt_id,)).fetchone()
                current_ver = row['version'] if row else None
                new_ver = bump_version(current_ver, bump_kind)
                conn.execute(
                    "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,(SELECT current_version_id FROM prompts WHERE id=?))",
                    (prompt_id, new_ver, content, ts, prompt_id)
                )
                compute_current_version(conn, prompt_id)
                prune_versions(conn, prompt_id)
            else:
                # 如果仅更新元信息，不动 versions，但若没有版本也创建一个
                row = conn.execute("SELECT COUNT(*) AS c FROM versions WHERE prompt_id=?", (prompt_id,)).fetchone()
                if row['c'] == 0:
                    conn.execute(
                        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,NULL)",
                        (prompt_id, '1.0.0', content, ts))
                    compute_current_version(conn, prompt_id)

            conn.commit()
            flash('已保存', 'success')
            return redirect(url_for('prompt_detail', prompt_id=prompt_id))

        # GET: 展示
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not prompt:
            flash('未找到该提示词', 'error')
            return redirect(url_for('index'))

        # 指定提示词密码模式：未解锁则跳转解锁页
        if auth_mode == 'per' and prompt['require_password']:
            unlocked = set(session.get('unlocked_prompts') or [])
            if prompt['id'] not in unlocked:
                return redirect(
                    url_for('unlock_prompt', prompt_id=prompt_id, next=url_for('prompt_detail', prompt_id=prompt_id)))

        versions = conn.execute("SELECT * FROM versions WHERE prompt_id=? ORDER BY created_at DESC",
                                (prompt_id,)).fetchall()
        current = conn.execute("SELECT * FROM versions WHERE id=?", (prompt['current_version_id'],)).fetchone() if \
        prompt['current_version_id'] else None

        return render_template('prompt_detail.html', prompt=prompt, versions=versions, current=current,
                               auth_mode=auth_mode)


@app.route('/prompt/<int:prompt_id>/pin', methods=['POST'])
def toggle_pin(prompt_id):
    with get_db() as conn:
        row = conn.execute("SELECT pinned FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if row:
            new_val = 0 if row['pinned'] else 1
            conn.execute("UPDATE prompts SET pinned=?, updated_at=? WHERE id=?", (new_val, now_ts(), prompt_id))
            conn.commit()
    return redirect(request.referrer or url_for('index'))


@app.route('/prompt/<int:prompt_id>/delete', methods=['POST'])
def delete_prompt(prompt_id):
    # 删除提示词：先删关联版本，再删提示词本身
    with get_db() as conn:
        row = conn.execute("SELECT id, name FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not row:
            flash('提示词不存在或已被删除', 'error')
            return redirect(url_for('index'))

        try:
            conn.execute("DELETE FROM versions WHERE prompt_id=?", (prompt_id,))
            conn.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
            conn.commit()
            flash('已删除提示词及其所有版本', 'success')
        except Exception:
            conn.rollback()
            flash('删除失败，请重试', 'error')
    return redirect(url_for('index'))


@app.route('/prompt/<int:prompt_id>/rollback/<int:version_id>', methods=['POST'])
def rollback_version(prompt_id, version_id):
    bump_kind = request.form.get('bump_kind', 'patch')
    with get_db() as conn:
        ver = conn.execute("SELECT * FROM versions WHERE id=? AND prompt_id=?", (version_id, prompt_id)).fetchone()
        if not ver:
            flash('版本不存在', 'error')
            return redirect(url_for('prompt_detail', prompt_id=prompt_id))
    # 计算新的版本号
    row = conn.execute("SELECT v.version FROM prompts p LEFT JOIN versions v ON v.id=p.current_version_id WHERE p.id=?",
                       (prompt_id,)).fetchone()
    current_ver = row['version'] if row else None
    new_ver = bump_version(current_ver, bump_kind)
    ts = now_ts()
    conn.execute(
        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,(SELECT current_version_id FROM prompts WHERE id=?))",
        (prompt_id, new_ver, ver['content'], ts, prompt_id)
    )
    compute_current_version(conn, prompt_id)
    prune_versions(conn, prompt_id)
    conn.commit()
    conn.close()
    flash('已从历史版本回滚并创建新版本', 'success')
    return redirect(url_for('prompt_detail', prompt_id=prompt_id))


@app.route('/toolbox')
def toolbox():
    return render_template('toolbox.html')


@app.route('/jasypt/encrypt', methods=['POST'])
def jasypt_encrypt_route():
    try:
        data = request.get_json()
        password = data.get('password', '')
        text = data.get('text', '')

        if not password:
            return jsonify({'error': '密码不能为空'}), 400

        if not text:
            return jsonify({'error': '待加密文本不能为空'}), 400

        encrypted = JasyptEncryptor.encrypt_with_config(text, password)
        return jsonify({'result': encrypted})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/jasypt/decrypt', methods=['POST'])
def jasypt_decrypt_route():
    try:
        data = request.get_json()
        password = data.get('password', '')
        encrypted_text = data.get('encrypted_text', '')

        if not password:
            return jsonify({'error': '密码不能为空'}), 400

        if not encrypted_text:
            return jsonify({'error': '待解密文本不能为空'}), 400

        decrypted = JasyptEncryptor.decrypt_with_config(encrypted_text, password)
        return jsonify({'result': decrypted})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        # 强制在受控块中解析表单，捕获解析异常，避免返回 400
        try:
            _ = request.form
        except BadRequest:
            flash('导入失败：上传表单解析错误', 'error')
            return redirect(url_for('settings'))

        with get_db() as conn:
            threshold = request.form.get('version_cleanup_threshold', '200').strip()
            if not threshold.isdigit() or int(threshold) < 1:
                flash('阈值需为正整数', 'error')
            else:
                set_setting(conn, 'version_cleanup_threshold', threshold)
                flash('设置已保存', 'success')
            # 语言设置
            language = (request.form.get('language') or 'zh').lower()
            if language not in ('zh', 'en'):
                language = 'zh'
            set_setting(conn, 'language', language)
            # 访问密码：模式 + 修改密码
            mode = request.form.get('auth_mode', 'off')
            if mode not in ('off', 'per', 'global'):
                mode = 'off'
            current_pw = (request.form.get('current_password') or '').strip()
            new_pw = (request.form.get('new_password') or '').strip()
            confirm_pw = (request.form.get('confirm_password') or '').strip()
            saved_hash = get_setting(conn, 'auth_password_hash', '') or ''
            prev_mode = get_setting(conn, 'auth_mode', 'off') or 'off'
            mode_to_set = mode
            # 当已存在密码时，调整认证相关设置（变更模式或修改密码）需要先验证当前密码
            auth_settings_changed = (mode != prev_mode) or bool(new_pw)
            if saved_hash and auth_settings_changed:
                if not current_pw:
                    flash('请先输入当前密码以修改认证设置', 'error')
                    mode_to_set = prev_mode
                elif hash_pw(current_pw) != saved_hash:
                    flash('当前密码不正确，无法修改认证设置', 'error')
                    mode_to_set = prev_mode
                else:
                    # 当前密码验证通过，允许继续
                    pass

            if mode != 'off':
                # 首次开启（尚未设置密码）必须设置新密码
                if not saved_hash and not new_pw:
                    flash('请先设置访问密码（4-8 位）', 'error')
                    mode_to_set = prev_mode  # 保持原状
                # 如用户输入了新密码，则校验并更新
                if new_pw:
                    if new_pw != confirm_pw:
                        flash('两次输入的密码不一致', 'error')
                        mode_to_set = prev_mode
                    elif not (4 <= len(new_pw) <= 8):
                        flash('密码长度需为 4-8 位', 'error')
                        mode_to_set = prev_mode
                    else:
                        set_setting(conn, 'auth_password_hash', hash_pw(new_pw))
            set_setting(conn, 'auth_mode', mode_to_set)

            # 导入（健壮性：捕获表单/JSON 解析异常，避免 400）
            try:
                files = request.files
            except BadRequest:
                # multipart 解析失败
                flash('导入失败：上传表单解析错误', 'error')
            else:
                if 'import_file' in files and files['import_file']:
                    try:
                        f = files['import_file']
                        data = json.load(f.stream)
                        # 覆盖所有数据
                        cur = conn.cursor()
                        cur.execute("DELETE FROM versions")
                        cur.execute("DELETE FROM prompts")
                        # 可包含 settings
                        if isinstance(data, dict) and 'prompts' in data:
                            prompts = data['prompts']
                        else:
                            prompts = data
                        for p in prompts:
                            cur.execute(
                                "INSERT INTO prompts(id, name, source, notes, color, tags, pinned, created_at, updated_at, current_version_id, require_password) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",
                                (
                                    p.get('id'),
                                    p.get('name'),
                                    p.get('source'),
                                    p.get('notes'),
                                    sanitize_color(p.get('color')),
                                    json.dumps(p.get('tags') or [], ensure_ascii=False),
                                    1 if p.get('pinned') else 0,
                                    p.get('created_at') or now_ts(),
                                    p.get('updated_at') or now_ts(),
                                    1 if p.get('require_password') else 0,
                                )
                            )
                            pid = cur.lastrowid if p.get('id') is None else p.get('id')
                            for v in (p.get('versions') or []):
                                cur.execute(
                                    "INSERT INTO versions(id, prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,?,?)",
                                    (
                                        v.get('id'),
                                        pid,
                                        v.get('version'),
                                        v.get('content') or '',
                                        v.get('created_at') or now_ts(),
                                        v.get('parent_version_id'),
                                    )
                                )
                            compute_current_version(conn, pid)
                        flash('已导入并覆盖所有数据', 'success')
                    except json.JSONDecodeError:
                        flash('导入失败：JSON 格式无效', 'error')
                    except Exception:
                        flash('导入失败，请重试', 'error')
        return redirect(url_for('settings'))

    # GET 请求，获取设置
    threshold = get_setting(None, 'version_cleanup_threshold', '200')
    auth_mode = get_setting(None, 'auth_mode', 'off') or 'off'
    has_password = bool(get_setting(None, 'auth_password_hash', '') or '')
    language = get_setting(None, 'language', LANG_DEFAULT) or LANG_DEFAULT
    return render_template('settings.html', threshold=threshold, auth_mode=auth_mode, has_password=has_password,
                           language=language)


@app.route('/export')
def export_all():
    with get_db() as conn:
        prompts = conn.execute("SELECT * FROM prompts ORDER BY id ASC").fetchall()
        result = []
        for p in prompts:
            versions = conn.execute("SELECT * FROM versions WHERE prompt_id=? ORDER BY created_at ASC",
                                    (p['id'],)).fetchall()
            result.append({
                'id': p['id'],
                'name': p['name'],
                'source': p['source'],
                'notes': p['notes'],
                'color': p['color'],
                'tags': json.loads(p['tags']) if p['tags'] else [],
                'pinned': bool(p['pinned']),
                'require_password': bool(p['require_password']) if 'require_password' in p.keys() else False,
                'created_at': p['created_at'],
                'updated_at': p['updated_at'],
                'current_version_id': p['current_version_id'],
                'versions': [
                    {
                        'id': v['id'],
                        'prompt_id': v['prompt_id'],
                        'version': v['version'],
                        'content': v['content'],
                        'created_at': v['created_at'],
                        'parent_version_id': v['parent_version_id'],
                    } for v in versions
                ]
            })
    payload = json.dumps({'prompts': result}, ensure_ascii=False, indent=2)
    bio = BytesIO(payload.encode('utf-8'))
    bio.seek(0)
    return send_file(bio, mimetype='application/json; charset=utf-8', as_attachment=True,
                     download_name='prompts_export.json')


# Diff 视图
from markupsafe import Markup, escape
import re
import difflib


def word_diff_html(a: str, b: str) -> str:
    # 先按行对齐，然后对每对行做词级 diff
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)
    rows = []

    def tokens(s):
        # 用词与空白/标点作为分隔，并保留分隔符
        return re.findall(r"\w+|\s+|[^\w\s]", s, flags=re.UNICODE)

    def wrap_span(cls, s):
        return Markup(f'<span class="{cls}">{escape(s)}</span>')

    def highlight_pair(al, bl):
        ta = tokens(al)
        tb = tokens(bl)
        sm2 = difflib.SequenceMatcher(None, ta, tb)
        ra = []
        rb = []
        for tag, i1, i2, j1, j2 in sm2.get_opcodes():
            if tag == 'equal':
                ra.append(escape(''.join(ta[i1:i2])))
                rb.append(escape(''.join(tb[j1:j2])))
            elif tag == 'delete':
                ra.append(wrap_span('diff-del', ''.join(ta[i1:i2])))
            elif tag == 'insert':
                rb.append(wrap_span('diff-ins', ''.join(tb[j1:j2])))
            else:  # replace
                ra.append(wrap_span('diff-del', ''.join(ta[i1:i2])))
                rb.append(wrap_span('diff-ins', ''.join(tb[j1:j2])))
        return Markup('').join(ra), Markup('').join(rb)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                left = escape(a_lines[i1 + k])
                right = escape(b_lines[j1 + k])
                rows.append((left, right, ''))
        elif tag == 'delete':
            for line in a_lines[i1:i2]:
                rows.append((wrap_span('diff-del', line), '', 'del'))
        elif tag == 'insert':
            for line in b_lines[j1:j2]:
                rows.append(('', wrap_span('diff-ins', line), 'ins'))
        else:  # replace
            al = a_lines[i1:i2]
            bl = b_lines[j1:j2]
            maxlen = max(len(al), len(bl))
            for k in range(maxlen):
                l = al[k] if k < len(al) else ''
                r = bl[k] if k < len(bl) else ''
                hl, hr = highlight_pair(l, r)
                rows.append((hl, hr, 'chg'))

    # 生成表格 HTML
    html = [
        '<table class="diff-table">',
        '<thead><tr><th>旧版本</th><th>新版本</th></tr></thead>',
        '<tbody>'
    ]
    for l, r, cls in rows:
        html.append(f'<tr class="{cls}"><td class="cell-left">{l}</td><td class="cell-right">{r}</td></tr>')
    html.append('</tbody></table>')
    return Markup('\n'.join(html))


def line_diff_html(a: str, b: str) -> str:
    # 使用 HtmlDiff 生成左右并排行级 diff
    d = difflib.HtmlDiff(wrapcolumn=120)
    html = d.make_table(a.splitlines(), b.splitlines(), context=False, numlines=0)
    # 包装简化，覆写样式类名以与全站风格一致
    # 将 difflib 输出的表格包在容器内
    return Markup(f'<div class="line-diff">{html}</div>')


@app.route('/prompt/<int:prompt_id>/diff')
def diff_view(prompt_id):
    left_id = request.args.get('left')
    right_id = request.args.get('right')
    mode = request.args.get('mode', 'word')  # word|line
    with get_db() as conn:
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        # 未解锁受保护提示词则跳转解锁
        auth_mode = get_setting(conn, 'auth_mode', 'off') or 'off'
        if auth_mode == 'per' and prompt and prompt['require_password'] and (
                prompt_id not in set(session.get('unlocked_prompts') or [])):
            return redirect(url_for('unlock_prompt', prompt_id=prompt_id,
                                    next=url_for('diff_view', prompt_id=prompt_id, left=left_id, right=right_id,
                                                 mode=mode)))
        versions = conn.execute("SELECT * FROM versions WHERE prompt_id=? ORDER BY created_at DESC",
                                (prompt_id,)).fetchall()
    if not versions:
        conn.close()
        flash('暂无版本', 'info')
        return redirect(url_for('prompt_detail', prompt_id=prompt_id))
    # 默认对比：上一版本 vs 当前版本
    if not right_id and prompt['current_version_id']:
        right_id = str(prompt['current_version_id'])
    if not left_id:
        # 找到 right 的前一个版本
        idx = 0
        for i, v in enumerate(versions):
            if str(v['id']) == str(right_id):
                idx = i
                break
        if idx + 1 < len(versions):
            left_id = str(versions[idx + 1]['id'])
        else:
            left_id = str(versions[idx]['id'])

    left = conn.execute("SELECT * FROM versions WHERE id=? AND prompt_id=?", (left_id, prompt_id)).fetchone()
    right = conn.execute("SELECT * FROM versions WHERE id=? AND prompt_id=?", (right_id, prompt_id)).fetchone()
    conn.close()
    if not left or not right:
        flash('所选版本不存在', 'error')
        return redirect(url_for('prompt_detail', prompt_id=prompt_id))

    if mode == 'line':
        diff_html = line_diff_html(left['content'], right['content'])
    else:
        diff_html = word_diff_html(left['content'], right['content'])

    return render_template('diff.html', prompt=prompt, versions=versions, left=left, right=right, mode=mode,
                           diff_html=diff_html)


@app.route('/prompt/<int:prompt_id>/versions')
def versions_page(prompt_id):
    with get_db() as conn:
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not prompt:
            flash('未找到该提示词', 'error')
            return redirect(url_for('index'))
        # 未解锁受保护提示词则跳转解锁
        auth_mode = get_setting(conn, 'auth_mode', 'off') or 'off'
        if auth_mode == 'per' and prompt['require_password'] and (
                prompt_id not in set(session.get('unlocked_prompts') or [])):
            return redirect(
                url_for('unlock_prompt', prompt_id=prompt_id, next=url_for('versions_page', prompt_id=prompt_id)))

        # Convert Row objects to dictionaries for JSON serialization
        versions = conn.execute("SELECT * FROM versions WHERE prompt_id=? ORDER BY created_at DESC",
                                (prompt_id,)).fetchall()
        versions_dict = [dict(version) for version in versions]

        current = conn.execute("SELECT * FROM versions WHERE id=?", (prompt['current_version_id'],)).fetchone() if \
        prompt['current_version_id'] else None
        current_dict = dict(current) if current else None

        prompt_dict = dict(prompt)

        return render_template('versions.html', prompt=prompt_dict, versions=versions_dict, current=current_dict)


@app.route('/api/tags')
def api_tags():
    with get_db() as conn:
        tags = get_all_tags(conn)
    return jsonify(tags)


# === 简易密码认证 ===
import hashlib
from urllib.parse import urlparse


def hash_pw(pw: str) -> str:
    return hashlib.sha256((pw or '').encode('utf-8')).hexdigest()


def _safe_next(default_path: str) -> str:
    """Return a safe relative next path.
    - If `next` is absent, return the provided default path.
    - If `next` contains an absolute URL with a different host, ignore it.
    - Always return a relative path (path + optional query).
    """
    raw = request.values.get('next')
    if not raw:
        return default_path
    try:
        p = urlparse(raw)
        # Disallow external redirects; only same-host or relative permitted
        if p.netloc and p.netloc != request.host:
            return default_path
        path = p.path or '/'
        query = ('?' + p.query) if p.query else ''
        # Ensure relative form
        if not path.startswith('/'):
            path = '/' + path
        return path + query
    except Exception:
        return default_path


@app.route('/login', methods=['GET', 'POST'])
def login():
    mode = get_setting(None, 'auth_mode', 'off') or 'off'
    saved_hash = get_setting(None, 'auth_password_hash', '') or ''
    nxt = _safe_next(url_for('index'))
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        if not (4 <= len(password) <= 8):
            flash('密码长度需为 4-8 位', 'error')
            return render_template('auth.html', mode=mode, action='login', next=nxt)
        if saved_hash and hash_pw(password) == saved_hash:
            session['auth_ok'] = True
            flash('已通过认证', 'success')
            return redirect(nxt)
        else:
            flash('密码不正确', 'error')
    return render_template('auth.html', mode=mode, action='login', next=nxt)


@app.route('/logout')
def logout():
    session.pop('auth_ok', None)
    session.pop('unlocked_prompts', None)
    flash('已退出登录', 'success')
    return redirect(url_for('index'))


@app.route('/prompt/<int:prompt_id>/unlock', methods=['GET', 'POST'])
def unlock_prompt(prompt_id):
    mode = get_setting(None, 'auth_mode', 'off') or 'off'
    saved_hash = get_setting(None, 'auth_password_hash', '') or ''
    with get_db() as conn:
        prompt = conn.execute("SELECT id, name FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if not prompt:
        flash('提示词不存在', 'error')
        return redirect(url_for('index'))
    nxt = _safe_next(url_for('prompt_detail', prompt_id=prompt_id))
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        if not (4 <= len(password) <= 8):
            flash('密码长度需为 4-8 位', 'error')
            return render_template('auth.html', mode=mode, action='unlock', prompt=prompt, next=nxt)
        if saved_hash and hash_pw(password) == saved_hash:
            unlocked = set(session.get('unlocked_prompts') or [])
            unlocked.add(prompt_id)
            session['unlocked_prompts'] = list(unlocked)
            flash('已解锁该提示词', 'success')
            return redirect(nxt)
        else:
            flash('密码不正确', 'error')
    return render_template('auth.html', mode=mode, action='unlock', prompt=prompt, next=nxt)


# ============================================================================
# AI 优化功能模块
# ============================================================================

class AIService:
    """AI 服务基类"""

    def __init__(self, config):
        self.config = config
        self.api_key = config['api_key']
        self.api_url = config.get('api_url')
        self.model_name = config.get('model_name', 'gpt-4')
        self.temperature = config.get('temperature', 0.7)
        self.max_tokens = config.get('max_tokens', 2000)
        self.system_prompt = config.get('system_prompt', '你是一个专业的提示词优化专家。')

    def optimize_prompt(self, original_prompt, optimization_instruction):
        """优化提示词的抽象方法"""
        raise NotImplementedError("子类必须实现此方法")

    def test_connection(self):
        """测试连接的抽象方法"""
        raise NotImplementedError("子类必须实现此方法")


class OpenAIService(AIService):
    """OpenAI 服务实现"""

    def __init__(self, config):
        super().__init__(config)
        self.api_url = self.api_url or "https://api.openai.com/v1/chat/completions"

    def optimize_prompt(self, original_prompt, optimization_instruction):
        """使用 OpenAI API 优化提示词"""
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"原始提示词：\n{original_prompt}\n\n优化要求：\n{optimization_instruction}"}
            ]

            data = {
                "model": self.model_name,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens
            }

            response = requests.post(self.api_url, headers=headers, json=data, timeout=30)

            if response.status_code == 200:
                result = response.json()
                return result['choices'][0]['message']['content'].strip()
            else:
                raise Exception(f"API 请求失败: {response.status_code} - {response.text}")

        except Exception as e:
            raise Exception(f"OpenAI 服务错误: {str(e)}")

    def test_connection(self):
        """测试 OpenAI 连接"""
        try:
            # 确保使用正确的endpoint
            if self.api_url.endswith('/chat/completions'):
                endpoint_url = self.api_url
            elif self.api_url.endswith('/'):
                endpoint_url = self.api_url + 'chat/completions'
            else:
                endpoint_url = self.api_url + '/chat/completions'

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            data = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1
            }

            response = requests.post(endpoint_url, headers=headers, json=data, timeout=10)
            return response.status_code == 200

        except Exception:
            return False


class ClaudeService(AIService):
    """Claude 服务实现"""

    def __init__(self, config):
        super().__init__(config)
        self.api_url = self.api_url or "https://api.anthropic.com/v1/messages"

    def optimize_prompt(self, original_prompt, optimization_instruction):
        """使用 Claude API 优化提示词"""
        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }

            user_message = f"原始提示词：\n{original_prompt}\n\n优化要求：\n{optimization_instruction}"

            data = {
                "model": self.model_name,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "system": self.system_prompt,
                "messages": [{"role": "user", "content": user_message}]
            }

            response = requests.post(self.api_url, headers=headers, json=data, timeout=30)

            if response.status_code == 200:
                result = response.json()
                return result['content'][0]['text'].strip()
            else:
                raise Exception(f"API 请求失败: {response.status_code} - {response.text}")

        except Exception as e:
            raise Exception(f"Claude 服务错误: {str(e)}")

    def test_connection(self):
        """测试 Claude 连接"""
        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }

            data = {
                "model": self.model_name,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "Hello"}]
            }

            response = requests.post(self.api_url, headers=headers, json=data, timeout=10)
            return response.status_code == 200

        except Exception:
            return False


def create_ai_service(config):
    """根据配置创建 AI 服务实例"""
    provider = config.get('provider', 'openai').lower()

    if provider == 'openai':
        return OpenAIService(config)
    elif provider == 'claude':
        return ClaudeService(config)
    else:
        raise ValueError(f"不支持的 AI 服务提供商: {provider}")


def encrypt_api_key(api_key):
    """简单的 API 密钥加密（Base64编码，可逆）"""
    # 在生产环境中，应该使用更安全的加密方法
    import base64
    return base64.b64encode(api_key.encode()).decode()


def decrypt_api_key(encrypted_key):
    """解密 API 密钥"""
    import base64
    try:
        return base64.b64decode(encrypted_key.encode()).decode()
    except Exception:
        return encrypted_key


def run_optimization_async(task_id):
    """异步执行优化任务"""
    with get_db() as conn:
        try:
            # 获取任务信息
            task = conn.execute('''
                SELECT ot.*, p.name as prompt_name, v.content as original_content, ac.*
                FROM optimization_tasks ot
                JOIN prompts p ON ot.prompt_id = p.id
                JOIN versions v ON ot.original_version_id = v.id
                JOIN ai_configs ac ON ot.ai_config_id = ac.id
                WHERE ot.id = ?
            ''', (task_id,)).fetchone()

            # Convert to dict to fix sqlite3.Row attribute access issues
            task = dict(task) if task else None

            if not task:
                raise Exception("任务不存在")

            # 更新任务状态为运行中
            conn.execute('UPDATE optimization_tasks SET status = ? WHERE id = ?', ('running', task_id))
            conn.commit()

            # 创建 AI 服务
            ai_config = {
                'api_key': decrypt_api_key(task['api_key']) if task['api_key'] else None,
                'api_url': task['api_url'],
                'model_name': task['model_name'],
                'temperature': task['temperature'],
                'max_tokens': task['max_tokens'],
                'system_prompt': task['system_prompt'],
                'provider': task['provider']
            }

            ai_service = create_ai_service(ai_config)

            # 执行优化
            optimized_content = ai_service.optimize_prompt(
                task['original_content'],
                task['optimization_prompt']
            )

            # 创建新版本
            try:
                current_version = float(task.get('current_version', '1.0'))
            except (ValueError, TypeError):
                current_version = 1.0
            new_version = f"{current_version + 0.1:.1f}"

            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO versions (prompt_id, version, content, created_at, parent_version_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (task['prompt_id'], new_version, optimized_content, now_ts(), task['original_version_id']))

            new_version_id = cursor.lastrowid

            # 更新提示词的当前版本
            conn.execute('UPDATE prompts SET current_version_id = ?, updated_at = ? WHERE id = ?',
                         (new_version_id, now_ts(), task['prompt_id']))

            # 创建版本关联记录
            cursor.execute('''
                INSERT INTO version_relations (original_version_id, optimized_version_id, task_id, created_at)
                VALUES (?, ?, ?, ?)
            ''', (task['original_version_id'], new_version_id, task_id, now_ts()))

            # 更新任务状态
            conn.execute('''
                UPDATE optimization_tasks
                SET status = ?, result_content = ?, updated_at = ?
            WHERE id = ?
            ''', ('completed', optimized_content, now_ts(), task_id))

            conn.commit()
            logger.info(f"优化任务完成: task_id={task_id}, prompt_id={task['prompt_id']}")
            return True

        except Exception as e:
            logger.error(f"优化任务失败 (ID: {task_id}): {e}")
            try:
                conn.execute('UPDATE optimization_tasks SET status = ?, error_message = ? WHERE id = ?',
                             ('failed', str(e), task_id))
                conn.commit()
            except:
                pass
            return False


# ============================================================================
# AI 相关路由
# ============================================================================

@app.route('/ai-configs')
def ai_configs():
    """AI 配置管理页面"""
    with get_db() as conn:
        configs = conn.execute('SELECT * FROM ai_configs ORDER BY created_at DESC').fetchall()
    return render_template('ai_configs_new.html', configs=configs)


@app.route('/test-delete')
def test_delete():
    """删除功能测试页面"""
    return render_template('test_delete.html')


@app.route('/api/ai-configs', methods=['GET', 'POST'])
def manage_ai_configs():
    """AI 配置 API"""
    if request.method == 'POST':
        data = request.get_json()

        if not data.get('name'):
            return jsonify({'error': '配置名称不能为空'}), 400

        # 加密 API 密钥
        encrypted_key = encrypt_api_key(data.get('api_key', '')) if data.get('api_key') else ''

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ai_configs (name, provider, model_name, api_key, api_url, system_prompt, temperature, max_tokens, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['name'],
                data.get('provider', 'openai'),
                data.get('model_name', 'gpt-4'),
                encrypted_key,
                data.get('api_url', ''),
                data.get('system_prompt', '你是一个专业的提示词优化专家。'),
                data.get('temperature', 0.7),
                data.get('max_tokens', 2000),
                now_ts(),
                now_ts()
            ))

            config_id = cursor.lastrowid
            conn.commit()

        return jsonify({'id': config_id, 'message': '配置创建成功'})

    else:
        with get_db() as conn:
            configs = conn.execute(
                'SELECT id, name, provider, model_name, api_url, system_prompt, temperature, max_tokens, is_active, created_at, updated_at FROM ai_configs ORDER BY created_at DESC').fetchall()

            return jsonify([dict(config) for config in configs])


@app.route('/api/ai-configs/<int:config_id>', methods=['GET', 'PUT', 'DELETE'])
def manage_ai_config(config_id):
    if request.method == 'GET':
        """获取单个 AI 配置"""
        with get_db() as conn:
            config = conn.execute('SELECT * FROM ai_configs WHERE id = ?', (config_id,)).fetchone()
            if not config:
                return jsonify({'error': '配置不存在'}), 404

            # 转换为字典，API密钥保持加密状态不返回
            config_dict = dict(config)
            if config_dict.get('api_key'):
                config_dict['api_key'] = '******'  # 隐藏真实API密钥
            else:
                config_dict['api_key'] = ''

            return jsonify(config_dict)

    elif request.method == 'PUT':
        return update_ai_config_logic(config_id)

    elif request.method == 'DELETE':
        return delete_ai_config_logic(config_id)


def delete_ai_config_logic(config_id):
    """删除 AI 配置"""
    with get_db() as conn:
        conn.execute('DELETE FROM ai_configs WHERE id = ?', (config_id,))
        conn.commit()
    return jsonify({'message': '配置删除成功'})


def update_ai_config_logic(config_id):
    """更新 AI 配置"""
    data = request.get_json()

    # 获取现有配置以保持原有API密钥
    with get_db() as conn:
        existing_config = conn.execute('SELECT * FROM ai_configs WHERE id = ?', (config_id,)).fetchone()
        if not existing_config:
            return jsonify({'error': '配置不存在'}), 404

    # 只在提供了新的API密钥时才更新，否则保持原有密钥
    if 'api_key' in data and data['api_key']:
        encrypted_key = encrypt_api_key(data['api_key'])
    else:
        encrypted_key = existing_config['api_key']

    # 构建动态更新语句，只更新提供的字段
    update_fields = []
    update_values = []

    # 处理各个字段，只有当字段在请求数据中时才更新
    if 'name' in data:
        update_fields.append('name = ?')
        update_values.append(data['name'])

    if 'provider' in data:
        update_fields.append('provider = ?')
        update_values.append(data['provider'])

    if 'model_name' in data:
        update_fields.append('model_name = ?')
        update_values.append(data['model_name'])

    # API密钥特殊处理：只在提供了新的API密钥时才更新
    if 'api_key' in data and data['api_key']:
        update_fields.append('api_key = ?')
        update_values.append(encrypted_key)
    elif 'api_key' not in data:
        # 如果请求中没有api_key字段，保持原有密钥不变
        pass

    if 'api_url' in data:
        update_fields.append('api_url = ?')
        update_values.append(data.get('api_url', ''))

    if 'system_prompt' in data:
        update_fields.append('system_prompt = ?')
        update_values.append(data.get('system_prompt', '你是一个专业的提示词优化专家。'))

    if 'temperature' in data:
        update_fields.append('temperature = ?')
        update_values.append(data.get('temperature', 0.7))

    if 'max_tokens' in data:
        update_fields.append('max_tokens = ?')
        update_values.append(data.get('max_tokens', 2000))

    # 添加更新时间和配置ID
    update_fields.append('updated_at = ?')
    update_values.append(now_ts())
    update_values.append(config_id)

    # 执行更新
    with get_db() as conn:
        conn.execute(f'''
            UPDATE ai_configs
            SET {', '.join(update_fields)}
            WHERE id = ?
        ''', update_values)
        conn.commit()
    return jsonify({'message': '配置更新成功'})


@app.route('/api/ai-configs/<int:config_id>/test', methods=['POST'])
@handle_database_errors
def test_ai_config(config_id):
    """测试 AI 配置连接"""
    with get_db() as conn:
        config = conn.execute('SELECT * FROM ai_configs WHERE id = ?', (config_id,)).fetchone()

        if not config:
            return jsonify({'error': '配置不存在'}), 404

        try:
            # 解密API密钥用于测试连接
            config_dict = dict(config)
            if config_dict.get('api_key'):
                config_dict['api_key'] = decrypt_api_key(config_dict['api_key'])

            ai_service = create_ai_service(config_dict)
            success = ai_service.test_connection()

            if success:
                return jsonify({
                    'message': '连接测试成功',
                    'success': True,
                    'provider': config['provider'],
                    'model': config['model_name']
                })
            else:
                return jsonify({
                    'error': '连接测试失败，请检查配置',
                    'success': False
                })

        except AIServiceError as e:
            logger.error(f"AI 配置测试失败 (ID: {config_id}): {e}")
            return jsonify({
                'error': f'AI 服务错误: {str(e)}',
                'success': False
            })
        except Exception as e:
            logger.error(f"AI 配置测试异常 (ID: {config_id}): {e}")
            return jsonify({
                'error': f'测试异常: {str(e)}',
                'success': False
            })


@app.route('/api/ai-configs/<int:config_id>/estimate', methods=['POST'])
@handle_database_errors
def estimate_optimization_cost(config_id):
    """估算优化成本"""
    with get_db() as conn:
        config = conn.execute('SELECT * FROM ai_configs WHERE id = ?', (config_id,)).fetchone()

        if not config:
            return jsonify({'error': '配置不存在'}), 404

        data = request.get_json()
        content_length = len(data.get('content', ''))

        try:
            cost_info = estimate_cost(dict(config), content_length)
            return jsonify({
                'success': True,
                'cost_info': cost_info
            })

        except Exception as e:
            logger.error(f"成本估算失败 (ID: {config_id}): {e}")
            return jsonify({
                'error': f'成本估算失败: {str(e)}',
                'success': False
            })


@app.route('/api/prompts/<int:prompt_id>/optimize', methods=['POST'])
@handle_database_errors
@handle_validation_errors
def create_optimization_task(prompt_id):
    """创建提示词优化任务"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请求数据格式错误'}), 400

    with get_db() as conn:
        # 获取提示词信息
        prompt = conn.execute('SELECT * FROM prompts WHERE id = ?', (prompt_id,)).fetchone()
        if not prompt:
            return jsonify({'error': '提示词不存在'}), 404

        # 获取当前版本内容
        current_version = conn.execute('''
            SELECT * FROM versions WHERE id = ?
        ''', (prompt['current_version_id'],)).fetchone()

        if not current_version:
            return jsonify({'error': '当前版本不存在'}), 404

        # 验证参数
        ai_config_id = data.get('ai_config_id', 1)
        optimization_prompt = data.get('optimization_prompt', '请优化这个提示词，使其更加有效和清晰。')

        # 验证 AI 配置是否存在
        ai_config = conn.execute('SELECT * FROM ai_configs WHERE id = ? AND is_active = 1', (ai_config_id,)).fetchone()
        if not ai_config:
            return jsonify({'error': 'AI 配置不存在或未激活'}), 400

        # 检查是否已有正在运行的优化任务
        running_task = conn.execute('''
            SELECT id FROM optimization_tasks
            WHERE prompt_id = ? AND status IN ('pending', 'running')
        ''', (prompt_id,)).fetchone()

        if running_task:
            return jsonify({
                'error': '该提示词已有正在运行的优化任务',
                'task_id': running_task['id']
            }), 409

        # 估算成本
        try:
            cost_info = estimate_cost(dict(ai_config), len(current_version['content']))
        except:
            cost_info = {'estimated_cost': 0.0, 'currency': 'USD'}

        # 创建优化任务
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO optimization_tasks (
                prompt_id, original_version_id, ai_config_id,
                optimization_prompt, created_at
            ) VALUES (?, ?, ?, ?, ?)
        ''', (
            prompt_id, current_version['id'], ai_config_id,
            optimization_prompt, now_ts()
        ))

        task_id = cursor.lastrowid
        conn.commit()

        logger.info(f"创建优化任务成功: task_id={task_id}, prompt_id={prompt_id}")

        # 异步执行优化任务
        import threading
        thread = threading.Thread(target=run_optimization_async, args=(task_id,))
        thread.daemon = True
        thread.start()

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': '优化任务创建成功',
            'cost_info': cost_info,
            'ai_config': {
                'provider': ai_config['provider'],
                'model': ai_config['model_name']
            }
        })


@app.route('/api/optimization-tasks/<int:task_id>')
@handle_database_errors
def get_optimization_task(task_id):
    """获取优化任务状态"""
    with get_db() as conn:
        task = conn.execute('''
        SELECT ot.*, p.name as prompt_name
        FROM optimization_tasks ot
        JOIN prompts p ON ot.prompt_id = p.id
        WHERE ot.id = ?
    ''', (task_id,)).fetchone()
    conn.close()

    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify(dict(task))


@app.route('/api/optimization-tasks/<int:task_id>/apply', methods=['POST'])
def apply_optimization_result(task_id):
    """应用优化结果"""
    with get_db() as conn:
        task = conn.execute('''
            SELECT * FROM optimization_tasks WHERE id = ? AND status = 'completed'
        ''', (task_id,)).fetchone()

        if not task:
            return jsonify({'error': '任务不存在或未完成'}), 404

        # 这里可以添加额外的应用逻辑
        # 例如：更新提示词的��他属性，发送通知等

        return jsonify({'message': '优化结果已应用'})


def run():
    ensure_db()
    app.run(host='0.0.0.0', port=3501, debug=True)


if __name__ == '__main__':
    run()
