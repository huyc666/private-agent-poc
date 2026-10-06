import random


def dice_roll(sides: int = 6) -> str:
    """掷一个 N 面骰子，返回 1 到 N 之间的随机整数结果。

    参数:
        sides: 骰子面数，默认为 6。必须为大于等于 1 的整数。

    返回:
        形如 "掷出 6 面骰子：4" 的字符串结果。
    """
    if not isinstance(sides, int):
        return f"错误：sides 必须是整数，收到 {type(sides).__name__}"
    if sides < 1:
        return f"错误：sides 必须大于等于 1，收到 {sides}"

    result = random.randint(1, sides)
    return f"掷出 {sides} 面骰子：{result}"
