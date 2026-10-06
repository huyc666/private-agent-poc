"""core-utils 平台包工具：calculator（ast 白名单安全求值，不执行任意代码）。"""
import ast
import operator

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("表达式包含不允许的元素")


def calculator(expression: str) -> str:
    """计算一个数学表达式，例如 "1024 * 768 + (3 ** 2)"。只支持 + - * / // % ** 与括号。"""
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
        return str(result)
    except Exception as e:
        return f"计算失败: {e}"
