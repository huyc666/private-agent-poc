FROM python:3.12-slim

# 出网封锁需要 iptables（见 entrypoint.sh）
RUN apt-get update && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

# 内网构建时把 pip 源换成内部镜像/清华镜像
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY server.py .
COPY entrypoint.sh .

# 非 root 运行（entrypoint.sh 配置完 iptables 后降权到该用户）
RUN useradd -m sandbox && chown -R sandbox /app && chmod +x entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
