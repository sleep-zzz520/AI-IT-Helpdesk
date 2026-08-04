# 后端镜像（python 运行时）
# 镜像源用国内加速（Docker Hub 在国内网络超时；网络正常可改回 python:3.12-slim）
FROM docker.m.daocloud.io/library/python:3.12-slim

WORKDIR /app

# 先拷依赖清单（利用层缓存：依赖不变不重装）
COPY requirements.txt .
# 国内网络用清华 PyPI 源（网络正常可去掉 -i 参数）
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 再拷代码（代码变更才触发后续层）
COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
