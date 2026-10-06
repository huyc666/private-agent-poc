"""core-utils 平台包工具：get_current_time。"""


def get_current_time() -> str:
    """获取当前服务器时间，返回 ISO 格式字符串。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
