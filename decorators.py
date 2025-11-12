"""
装饰器模块 - 提供通用的装饰器功能
"""
import logging
import sqlite3
import json
from functools import wraps
from flask import jsonify, session, request, redirect, url_for
from werkzeug.exceptions import BadRequest

logger = logging.getLogger(__name__)

def handle_database_errors(f):
    """数据库错误处理装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except sqlite3.Error as e:
            logger.error(f"Database error in {f.__name__}: {e}")
            return jsonify({'error': '数据库操作失败，请稍后重试'}), 500
        except Exception as e:
            logger.error(f"Unexpected error in {f.__name__}: {e}")
            return jsonify({'error': '服务器内部错误'}), 500
    return decorated_function

def handle_validation_errors(f):
    """验证错误处理装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except BadRequest as e:
            logger.warning(f"Validation error in {f.__name__}: {e}")
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Unexpected error in {f.__name__}: {e}")
            return jsonify({'error': '请求处理失败'}), 500
    return decorated_function

def require_auth(auth_mode='per'):
    """认证装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if auth_mode == 'global' and not session.get('auth_ok'):
                # 记录原始请求URL，登录后跳转回来
                nxt = request.full_path if request.query_string else request.path
                nxt = nxt.rstrip('?')
                return redirect(url_for('login', next=nxt))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def log_api_calls(f):
    """API调用日志装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        try:
            result = f(*args, **kwargs)
            duration = time.time() - start_time

            # 记录成功的API调用
            logger.info(f"API {f.__name__}: {request.method} {request.path} - "
                       f"Status: {getattr(result, 'status_code', 200)} - "
                       f"Duration: {duration:.3f}s")
            return result
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"API {f.__name__}: {request.method} {request.path} - "
                        f"Error: {str(e)} - Duration: {duration:.3f}s")
            raise
    return decorated_function

def rate_limit(max_requests=100, window_seconds=3600):
    """简单的速率限制装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 这里可以实现基于Redis或内存的速率限制
            # 为了简单起见，这里只是一个占位符
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def validate_json_content_type(f):
    """验证JSON内容类型装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ['POST', 'PUT', 'PATCH']:
            if not request.is_json:
                try:
                    # 尝试解析表单数据中的JSON
                    if request.form:
                        return f(*args, **kwargs)
                except:
                    pass

                return jsonify({'error': '请求必须是JSON格式'}), 400
        return f(*args, **kwargs)
    return decorated_function

def cache_response(timeout=300):
    """响应缓存装饰器（简单实现）"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 这里可以实现基于内存或Redis的缓存
            # 为了简单起见，这里只是一个占位符
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# 导入time模块
import time