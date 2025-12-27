# 1. 使用輕量級 Linux + Python 3.9
FROM python:3.9-slim

# 2. 更新系統並安裝 g++, clang, 和 stdbuf (coreutils)
# 這些是你編譯 C++ 和處理緩衝區所必須的工具
RUN apt-get update && apt-get install -y \
    g++ \
    clang \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

# 3. 設定工作目錄
WORKDIR /app

# 4. 複製你的檔案進去
COPY . .

# 5. 安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 6. 設定環境變數 (讓 Python 輸出更即時)
ENV PYTHONUNBUFFERED=1

# 7. 啟動指令
CMD ["python", "server.py"]
