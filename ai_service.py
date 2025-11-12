"""
AI 服务模块 - 处理各种 AI 提供商的集成
"""
import json
import logging
import time
import requests
from typing import Dict, Any, Optional
from functools import wraps

logger = logging.getLogger(__name__)

class AIServiceError(Exception):
    """AI 服务异常"""
    pass

def retry_on_failure(max_retries=3, delay=1.0, backoff=2.0):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"AI 服务调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(f"AI 服务调用失败，已重试 {max_retries} 次: {e}")

            raise last_exception
        return wrapper
    return decorator

class OpenAIService:
    """OpenAI 服务实现"""

    def __init__(self, config: Dict[str, Any]):
        self.api_key = config.get('api_key')
        self.api_url = config.get('api_url', 'https://api.openai.com/v1/chat/completions')
        self.model_name = config.get('model_name', 'gpt-4')
        self.temperature = config.get('temperature', 0.7)
        self.max_tokens = config.get('max_tokens', 2000)
        self.system_prompt = config.get('system_prompt', '你是一个专业的提示词优化专家')
        self.timeout = 30

    def test_connection(self) -> bool:
        """测试连接"""
        try:
            # 确保使用正确的endpoint
            if self.api_url.endswith('/chat/completions'):
                endpoint_url = self.api_url
            elif self.api_url.endswith('/'):
                endpoint_url = self.api_url + 'chat/completions'
            else:
                endpoint_url = self.api_url + '/chat/completions'

            response = requests.post(
                endpoint_url,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': self.model_name,
                    'messages': [{'role': 'user', 'content': 'test'}],
                    'max_tokens': 1
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"OpenAI 连接测试失败: {e}")
            return False

    @retry_on_failure(max_retries=3, delay=1.0)
    def optimize_prompt(self, original_content: str, optimization_prompt: str) -> str:
        """优化提示词"""
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        messages = [
            {'role': 'system', 'content': self.system_prompt},
            {'role': 'user', 'content': f"原始提示词：\n{original_content}\n\n优化要求：\n{optimization_prompt}"}
        ]

        data = {
            'model': self.model_name,
            'messages': messages,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens
        }

        try:
            # 确保使用正确的endpoint
            if self.api_url.endswith('/chat/completions'):
                endpoint_url = self.api_url
            elif self.api_url.endswith('/'):
                endpoint_url = self.api_url + 'chat/completions'
            else:
                endpoint_url = self.api_url + '/chat/completions'

            response = requests.post(
                endpoint_url,
                headers=headers,
                json=data,
                timeout=self.timeout
            )
            response.raise_for_status()

            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                return result['choices'][0]['message']['content'].strip()
            else:
                raise AIServiceError("AI 服务返回格式异常")

        except requests.exceptions.Timeout:
            raise AIServiceError("AI 服务请求超时")
        except requests.exceptions.RequestException as e:
            raise AIServiceError(f"AI 服务请求失败: {e}")
        except json.JSONDecodeError:
            raise AIServiceError("AI 服务响应解析失败")
        except Exception as e:
            raise AIServiceError(f"AI 服务处理失败: {e}")

class ClaudeService:
    """Claude 服务实现"""

    def __init__(self, config: Dict[str, Any]):
        self.api_key = config.get('api_key')
        self.api_url = config.get('api_url', 'https://api.anthropic.com/v1/messages')
        self.model_name = config.get('model_name', 'claude-3-sonnet-20240229')
        self.temperature = config.get('temperature', 0.7)
        self.max_tokens = config.get('max_tokens', 2000)
        self.system_prompt = config.get('system_prompt', '你是一个专业的提示词优化专家')
        self.timeout = 30

    def test_connection(self) -> bool:
        """测试连接"""
        try:
            # Claude API通常使用完整的messages endpoint
            if self.api_url.endswith('/messages'):
                endpoint_url = self.api_url
            elif self.api_url.endswith('/'):
                endpoint_url = self.api_url + 'messages'
            else:
                endpoint_url = self.api_url + '/messages'

            response = requests.post(
                endpoint_url,
                headers={
                    'x-api-key': self.api_key,
                    'Content-Type': 'application/json',
                    'anthropic-version': '2023-06-01'
                },
                json={
                    'model': self.model_name,
                    'max_tokens': 1,
                    'messages': [{'role': 'user', 'content': 'test'}]
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Claude 连接测试失败: {e}")
            return False

    @retry_on_failure(max_retries=3, delay=1.0)
    def optimize_prompt(self, original_content: str, optimization_prompt: str) -> str:
        """优化提示词"""
        headers = {
            'x-api-key': self.api_key,
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01'
        }

        messages = [
            {'role': 'user', 'content': f"原始提示词：\n{original_content}\n\n优化要求：\n{optimization_prompt}"}
        ]

        data = {
            'model': self.model_name,
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
            'system': self.system_prompt,
            'messages': messages
        }

        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json=data,
                timeout=self.timeout
            )
            response.raise_for_status()

            result = response.json()
            if 'content' in result and len(result['content']) > 0:
                return result['content'][0]['text'].strip()
            else:
                raise AIServiceError("Claude 服务返回格式异常")

        except requests.exceptions.Timeout:
            raise AIServiceError("Claude 服务请求超时")
        except requests.exceptions.RequestException as e:
            raise AIServiceError(f"Claude 服务请求失败: {e}")
        except json.JSONDecodeError:
            raise AIServiceError("Claude 服务响应解析失败")
        except Exception as e:
            raise AIServiceError(f"Claude 服务处理失败: {e}")

class LocalAIService:
    """本地 AI 服务（Ollama 等）"""

    def __init__(self, config: Dict[str, Any]):
        self.api_url = config.get('api_url', 'http://localhost:11434/api/generate')
        self.model_name = config.get('model_name', 'llama2')
        self.system_prompt = config.get('system_prompt', '你是一个专业的提示词优化专家')
        self.timeout = 60

    def test_connection(self) -> bool:
        """测试连接"""
        try:
            response = requests.post(
                self.api_url,
                json={
                    'model': self.model_name,
                    'prompt': 'test',
                    'stream': False
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"本地 AI 服务连接测试失败: {e}")
            return False

    @retry_on_failure(max_retries=2, delay=2.0)
    def optimize_prompt(self, original_content: str, optimization_prompt: str) -> str:
        """优化提示词"""
        prompt = f"""System: {self.system_prompt}

User: 原始提示词：{original_content}

优化要求：{optimization_prompt}

请提供优化后的提示词："""

        try:
            response = requests.post(
                self.api_url,
                json={
                    'model': self.model_name,
                    'prompt': prompt,
                    'stream': False,
                    'options': {
                        'temperature': 0.7,
                        'num_predict': 2000
                    }
                },
                timeout=self.timeout
            )
            response.raise_for_status()

            result = response.json()
            if 'response' in result:
                return result['response'].strip()
            else:
                raise AIServiceError("本地 AI 服务返回格式异常")

        except requests.exceptions.Timeout:
            raise AIServiceError("本地 AI 服务请求超时")
        except requests.exceptions.RequestException as e:
            raise AIServiceError(f"本地 AI 服务请求失败: {e}")
        except json.JSONDecodeError:
            raise AIServiceError("本地 AI 服务响应解析失败")
        except Exception as e:
            raise AIServiceError(f"本地 AI 服务处理失败: {e}")

def create_ai_service(config: Dict[str, Any]):
    """AI 服务工厂函数"""
    provider = config.get('provider', 'openai').lower()

    if provider == 'openai':
        return OpenAIService(config)
    elif provider == 'claude':
        return ClaudeService(config)
    elif provider == 'local':
        return LocalAIService(config)
    else:
        raise ValueError(f"不支持的 AI 服务提供商: {provider}")

def estimate_cost(config: Dict[str, Any], content_length: int) -> Dict[str, Any]:
    """估算 API 调用成本"""
    provider = config.get('provider', 'openai').lower()
    model_name = config.get('model_name', '')

    # 简单的成本估算（实际需要根据具体定价计算）
    if provider == 'openai':
        if 'gpt-4' in model_name:
            cost_per_token = 0.00003  # 示例价格
        elif 'gpt-3.5' in model_name:
            cost_per_token = 0.000002
        else:
            cost_per_token = 0.00001
    elif provider == 'claude':
        if 'claude-3-opus' in model_name:
            cost_per_token = 0.000075
        elif 'claude-3-sonnet' in model_name:
            cost_per_token = 0.000015
        else:
            cost_per_token = 0.00001
    else:
        cost_per_token = 0.0  # 本地服务免费

    estimated_input_tokens = content_length // 4  # 粗略估算
    estimated_output_tokens = 500  # 估算输出长度
    total_tokens = estimated_input_tokens + estimated_output_tokens

    return {
        'estimated_tokens': total_tokens,
        'estimated_cost': total_tokens * cost_per_token,
        'currency': 'USD'
    }