#!/usr/bin/env bash
# 一键环境安装脚本：pyenv → Python 3.12 → .venv → 后端依赖
# 用法：在项目根目录执行  bash scripts/setup_env.sh
# 说明：需要在【你自己的终端】运行（本脚本要写 ~/.pyenv 等系统目录）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/5] 安装 pyenv"
if ! command -v pyenv >/dev/null 2>&1; then
  echo "    未检测到 pyenv，通过 Homebrew 安装..."
  brew install pyenv
  # 写入 zsh 配置（macOS 默认 shell）
  cat >> ~/.zshrc <<'EOF'

# pyenv
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
EOF
  echo "    已把 pyenv 配置写入 ~/.zshrc"
fi
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
echo "    pyenv $(pyenv --version)"

echo "==> [2/5] 查找并安装 Python 3.12（编译需几分钟，请耐心）"
PY_VER=$(pyenv install -l | sed 's/^[[:space:]]*//' | grep -E '^3\.12\.[0-9]+$' | tail -1)
echo "    目标版本: $PY_VER"
pyenv install -s "$PY_VER"
pyenv local "$PY_VER"
echo "    当前 Python: $(python3 --version) @ $(command -v python3)"

echo "==> [3/5] 创建虚拟环境 .venv"
python3 -m venv .venv
source .venv/bin/activate
echo "    venv 创建完成"

echo "==> [4/5] 安装后端依赖"
pip install --upgrade pip -q
pip install -r backend/requirements.txt -q
echo "    依赖安装完成"

echo "==> [5/5] 验证"
python3 - <<'PY'
import langgraph, fastapi, sqlalchemy, openai, pymysql
print(f"    langgraph {langgraph.__version__} | fastapi {fastapi.__version__} | sqlalchemy {sqlalchemy.__version__}")
PY

echo ""
echo "=============================================="
echo "✅ 环境就绪！"
echo "    以后每次开发前先执行：source .venv/bin/activate"
echo "=============================================="
