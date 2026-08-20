from __future__ import annotations


class IndexerError(Exception):
    """Indexer domain error with a stable public code and safe message."""

    code = "indexer_error"
    default_public_message = "资源索引服务暂不可用"

    def __init__(self, detail: str = "", *, public_message: str | None = None):
        super().__init__(detail or self.default_public_message)
        self.detail = detail
        self.public_message = public_message or self.default_public_message


class IndexerValidationError(IndexerError, ValueError):
    code = "validation_error"
    default_public_message = "搜索参数无效"


class IndexerSecurityError(IndexerError):
    code = "security_error"
    default_public_message = "上游地址未通过安全校验"


class IndexerResponseTooLarge(IndexerError):
    code = "response_too_large"
    default_public_message = "上游响应超过大小限制"


class IndexerInvalidResponse(IndexerError):
    code = "invalid_response"
    default_public_message = "上游返回了无法识别的数据"


class IndexerUnavailable(IndexerError):
    code = "unavailable"
    default_public_message = "索引站点暂不可用"


class IndexerTimeout(IndexerError):
    code = "timeout"
    default_public_message = "索引站点响应超时"


class IndexerRateLimited(IndexerError):
    code = "rate_limited"
    default_public_message = "索引站点请求过于频繁"


class IndexerResultNotFound(IndexerError, LookupError):
    code = "result_not_found"
    default_public_message = "资源结果不存在"


class IndexerResultExpired(IndexerError, LookupError):
    code = "result_expired"
    default_public_message = "资源结果已过期"
