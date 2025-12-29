# 使用 Python 3.9 輕量版作為基底
FROM python:3.9-slim

# 1. 更新系統並安裝編譯 C++ 所需的工具
RUN apt-get update && apt-get install -y \
    g++ \
    clang \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

# 2. 設定工作目錄
WORKDIR /app

# 3. 將當前目錄的所有檔案複製到容器內
COPY . .

# 4. 安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 5. 設定環境變數
ENV PYTHONUNBUFFERED=1

# 6. 啟動伺服器
CMD ["python", "server.py"]
