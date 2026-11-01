"""因子层：沙箱执行 + 内置因子库。"""
from .sandbox import FactorError, check_lookahead, run_factor, validate_factor_code

__all__ = ["FactorError", "check_lookahead", "run_factor", "validate_factor_code"]