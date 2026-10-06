#!/bin/sh
set -e

# ---- 出网封锁（在容器网络命名空间内生效）----
# 只放行回环与「入站连接的响应流量」，其余出站全部丢弃。
# 效果：run_python_code / run_tool 在容器内执行的不可信代码无法访问外网或内网，
# 而宿主机经 127.0.0.1:9001 发布的入站连接不受影响（走 INPUT 路径）。
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -j DROP

# 降到非 root 沙箱用户运行服务（子进程同用户，且除 NET_ADMIN 外无任何 capabilities）
exec su sandbox -s /bin/sh -c "uvicorn server:app --host 0.0.0.0 --port 9000"
