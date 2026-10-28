#!/bin/bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
cd "$(dirname "$0")/../.."
fail() { printf '%s\n' "$1"; read -r -p '按回车关闭。' || true; exit 1; }
python_bin=''
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if "$candidate" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    python_bin="$candidate"; break
  fi
done
[ -n "$python_bin" ] || fail '请先安装 Python 3.10 或更新版本，再双击本文件。安装方法见 README。'
app_data="${HOME}/Library/Application Support/Pawzz Video"
mkdir -p "$app_data"
chmod 700 "$app_data"
if [ ! -x "$app_data/venv/bin/python" ]; then
  "$python_bin" -m venv "$app_data/venv" || fail '运行环境创建失败，请检查 Python 安装和目录权限。'
fi
if ! "$app_data/venv/bin/python" -c 'import importlib.metadata; assert importlib.metadata.version("paho-mqtt") == "2.1.0"; assert importlib.metadata.version("cryptography") == "50.0.1"' >/dev/null 2>&1; then
  printf '%s\n' '首次启动：正在准备导出所需的小型依赖，请稍等。'
  "$app_data/venv/bin/python" -m pip install -r requirements.txt || fail '依赖下载失败，请检查网络后重试。'
fi
"$app_data/venv/bin/python" src/local_app.py "$@" || fail '服务启动失败，请检查上方提示。'
