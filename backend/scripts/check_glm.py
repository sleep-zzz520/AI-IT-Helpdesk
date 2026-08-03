"""GLM 连通性验证脚本。

用法（在 backend 目录下）：
    cd backend && python -m scripts.check_glm

注意：必须用 -m 从 backend 根目录运行，不要直接 python scripts/check_glm.py——
直接跑会把脚本所在目录（backend/scripts/）加入 sys.path，找不到 app 包。

预期输出：GLM 响应: <模型返回的一句话>
"""
from app.llm import chat

if __name__ == "__main__":
    reply = chat([{"role": "user", "content": "用一句话介绍你自己"}])
    print("GLM 响应:", reply)
