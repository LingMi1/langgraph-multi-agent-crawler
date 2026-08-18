# ============================================================================
# 企业级 AI Agent 爬虫 Docker 镜像 (Phase 3)
# 基于 python:3.11-slim，包含 Playwright Chromium 和所有运行时依赖
# ============================================================================

FROM python:3.11-slim

LABEL maintainer="agent-crawler"
LABEL description="Enterprise AI Agent Crawler with ReAct + HITL + Supervisor"

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# ==================== 安装系统依赖 ====================
# Playwright 所需系统库 + 基础工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium 依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libx11-xcb1 \
    libxcursor1 \
    libxext6 \
    libxi6 \
    libxrender1 \
    libxtst6 \
    # 网络工具
    curl \
    ca-certificates \
    # 清理 apt 缓存
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/*

# ==================== 安装 Python 依赖 ====================
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# ==================== 安装 Playwright Chromium ====================
RUN python -m playwright install chromium \
    && python -m playwright install-deps chromium

# ==================== 复制项目代码 ====================
COPY . .

# ==================== 创建数据目录 ====================
RUN mkdir -p /app/output /app/csv_output /app/data

# ==================== 暴露端口（如有 Web 服务可扩展） ====================
EXPOSE 8080

# ==================== 默认启动命令 ====================
CMD ["python", "main.py"]