FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# config/data/logs/.env 由宿主机以卷挂载，镜像内不携带真实配置
RUN mkdir -p /app/config /app/data /app/logs

EXPOSE 8788

# 默认跑 WebSocket 监听 bot；管理页由 docker-compose 的 admin 服务用
#   python -m app.control
# 覆盖 command 启动
CMD ["python", "-m", "app.main"]
