# FastAPI 服务化镜像 — 多 Agent 爬虫 REST 服务
# 构建：  docker build -t crawler-api .
# 运行：  docker run -p 8000:8000 -e CRAWLER_API_KEY=secret -v crawler_out:/app/output crawler-api
# 注意：  Playwright/系统 Chrome 未打进镜像（体积与内网约束），JS 渲染站点走 httpx
#         降级路径；纯静态/BS4 站点完整可用。
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

EXPOSE 8000
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
