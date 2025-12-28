FROM python:3.9-slim

# 安裝系統工具與 C++ 編譯器
RUN apt-get update && apt-get install -y \
    g++ \
    clang \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 複製當前目錄所有檔案到容器中
COPY . .

# 安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 設定環境變數
ENV PYTHONUNBUFFERED=1

# 啟動指令
CMD ["python", "server.py"]
