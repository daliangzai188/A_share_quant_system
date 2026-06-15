"""Windows 环境一键安装依赖，运行一次即可：py -3.11 setup_windows.py"""
import subprocess
import sys

packages = [
    "pandas>=2.0.0",
    "numpy>=1.24.0",
    "tushare>=1.4.0",
    "python-dotenv>=1.0.0",
    "requests>=2.31.0",
    "tenacity>=8.2.0",
    "openpyxl>=3.1.0",
    "tabulate>=0.9.0",
]

print(f"使用 Python: {sys.executable}")
print(f"安装 {len(packages)} 个依赖包...\n")

result = subprocess.run(
    [sys.executable, "-m", "pip", "install", "--upgrade"] + packages,
    check=False,
)

if result.returncode == 0:
    print("\n✅ 所有依赖安装成功，现在可以运行 start_windows.py")
else:
    print("\n❌ 部分依赖安装失败，请查看上方错误信息")
    sys.exit(1)
