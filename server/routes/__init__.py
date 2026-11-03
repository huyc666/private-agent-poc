"""按资源域拆分的路由（编码规范批 4）：端点实现自 server/main.py 原样搬迁，
装饰器路径/方法/依赖注入语义不变；server.main 经 register(app) 一次性挂载。
分批落地中：本 commit 已挂载 admin_users、files、sessions。"""


def register(app) -> None:
    """集中挂载各资源域路由。函数内延迟 import：规避脚本启动
    （python server/main.py）时 routes ↔ server.main 的循环导入。"""
    from . import (admin_users, files, sessions)
    for m in (admin_users, files, sessions):
        app.include_router(m.router)
