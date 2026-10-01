class DomainError(Exception):
    """違反業務規則；訊息會直接顯示給使用者。"""


class PermissionDenied(DomainError):
    pass
