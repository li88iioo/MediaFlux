"""目录刮削可公开业务错误边界。"""
from __future__ import annotations


class DirectoryScrapePublicError(Exception):
    """消息经过代码审查，可安全返回给用户。"""

    status_code = 400


class DirectoryScrapeRequestError(ValueError, DirectoryScrapePublicError):
    """请求参数或所选对象不满足刮削前置条件。"""

    status_code = 400


class DirectoryScrapeConflictError(RuntimeError, DirectoryScrapePublicError):
    """预览、规则或云端内容发生可恢复的状态冲突。"""

    status_code = 409


class DirectoryScrapeStateError(ValueError, DirectoryScrapePublicError):
    """配置或所选云端对象状态不满足执行条件。"""

    status_code = 409


class DirectoryScrapeGoneError(KeyError, DirectoryScrapePublicError):
    """短期检查/预览记录已不存在。"""

    status_code = 410


def public_error_message(exc: DirectoryScrapePublicError) -> str:
    """避免 ``KeyError.__str__`` 自动增加引号。"""
    if exc.args:
        return str(exc.args[0])
    return "目录刮削请求失败"

def safe_organize_failure(exc: Exception) -> str:
    """只向用户暴露经过审查的目录刮削错误，其余异常统一收敛。"""
    if isinstance(exc, DirectoryScrapePublicError):
        return public_error_message(exc)
    return "文件整理失败，请稍后重试"
