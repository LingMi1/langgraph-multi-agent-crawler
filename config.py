"""
项目配置文件
所有配置项统一管理，支持环境变量覆盖
"""

import os
from dotenv import load_dotenv

# 自动加载项目根目录下的 .env 文件
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ==================== DeepSeek API 配置 ====================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
# 部分内网网关的 deepseek 系模型默认开启 thinking 推理模式，
# 其 reasoning_content 在多轮工具调用续传时会 400（须回传）。
# 置 true 时请求显式禁用 thinking，保证 FunctionCallingLoop/FC 裁决正常续传。
DEEPSEEK_DISABLE_THINKING = os.getenv("DEEPSEEK_DISABLE_THINKING", "false").lower() in ("true", "1", "yes")

# ==================== LLM 多 provider 故障转移 ====================
# 主 provider 见上方 DEEPSEEK_*；备用 provider 的 base_url 以 | 分隔。
# 备用 key / model 可选：缺省复用主配置；若提供则与 base_url 按下标一一对应。
# 主 provider 重试耗尽后自动切换备用，全部失败才记熔断失败。
LLM_BACKUP_BASE_URLS = os.getenv("LLM_BACKUP_BASE_URLS", "")
LLM_BACKUP_API_KEYS = os.getenv("LLM_BACKUP_API_KEYS", "")
LLM_BACKUP_MODELS = os.getenv("LLM_BACKUP_MODELS", "")


def get_model_name() -> str:
    """返回规范化后的模型名（DeepSeek API 要求全小写，如 deepseek-v4-pro）"""
    return (DEEPSEEK_MODEL or "deepseek-chat").lower()

# ==================== MySQL 数据库配置 ====================
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1234")
DB_NAME = os.getenv("DB_NAME", "company_spider")
DB_TABLE = os.getenv("DB_TABLE", "scraped_pages")

# ==================== 存储模式配置 ====================
# "local"  = 仅本地保存HTML文件（不连数据库）
# "mysql"  = 存入MySQL数据库（需要MySQL服务）
# "both"   = 同时存入MySQL + 本地备份
SAVE_MODE = os.getenv("SAVE_MODE", "local").lower()

# ==================== 全站爬取输出配置 ====================
# 输出根目录（自动在下面创建"域名/多级目录/"结构）
LOCAL_BACKUP_DIR = os.getenv("LOCAL_BACKUP_DIR", "output")

# ==================== 爬虫配置 ====================
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.5"))  # 请求间隔（秒），避免被封
# TLS 证书校验（默认开启；内网自签证书站可设 false 显式豁免，默认安全）
CRAWLER_TLS_VERIFY = os.getenv("CRAWLER_TLS_VERIFY", "true").lower() in ("true", "1", "yes")
# robots.txt 合规（默认开启；遵守站点 robots.txt 的 User-agent: * 通配段，请求失败/缺失放行）
CRAWLER_RESPECT_ROBOTS = os.getenv("CRAWLER_RESPECT_ROBOTS", "true").lower() in ("true", "1", "yes")
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "5"))              # 嵌套遍历最大深度（默认5层）
MAX_NAV_DEPTH = int(os.getenv("MAX_NAV_DEPTH", "4"))      # 最大导航级别（1-4级）
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))            # 单次任务最大采集URL数
IMAGE_DOWNLOAD = os.getenv("IMAGE_DOWNLOAD", "false").lower() in ("true", "1", "yes")  # 是否下载图片到本地
IMAGE_INLINE_MODE = os.getenv("IMAGE_INLINE_MODE", "true").lower() in ("true", "1", "yes")  # ★ 是否将图片转为 Base64 内嵌（实现离线完美显示）
IMAGE_MAX_INLINE_SIZE = int(os.getenv("IMAGE_MAX_INLINE_SIZE", "2097152"))  # ★ Base64 单图最大字节（默认2MB），超过则跳过
MAX_RETRY_COUNT = int(os.getenv("MAX_RETRY_COUNT", "3"))  # ★ 失败 URL 最大重试次数
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ==================== CSV 输出配置 ====================
CSV_OUTPUT_DIR = os.getenv("CSV_OUTPUT_DIR", "csv_output")
SYS_PLATFORM = os.getenv("SYS_PLATFORM", "10000010")

# ==================== 内容质量过滤配置 ====================
# 提取的可见文本少于该字符数时跳过保存（设为 0 关闭过滤）
CONTENT_QUALITY_MIN_CHARS = int(os.getenv("CONTENT_QUALITY_MIN_CHARS", "100"))
# 是否启用内容质量过滤（false / 0 关闭）
CONTENT_QUALITY_FILTER_ENABLED = os.getenv("CONTENT_QUALITY_FILTER_ENABLED", "true").lower() in ("true", "1", "yes")

# ==================== 自适应 LLM 清洗配置（分层方案 Tier3） ====================
# off     = 不使用（保持现状，纯规则清洗）
# dry_run = 只计算质量分并记录疑难页 reason，不升级（用于标定阈值）
# on      = 质量分不合格的页面升级整篇 LLM 清洗（疑难页才花一次全篇 LLM）
ADAPTIVE_LLM_CLEAN = os.getenv("ADAPTIVE_LLM_CLEAN", "off").lower()
# 质量分阈值（0-1，低于该值视为疑难页，触发升级）。分数越低质量越差。
ADAPTIVE_LLM_QUALITY_THRESHOLD = float(os.getenv("ADAPTIVE_LLM_QUALITY_THRESHOLD", "0.6"))
# 单次运行升级上限：疑难页过多时熔断，防止 LLM 被全篇清洗打爆
ADAPTIVE_LLM_MAX_UPGRADES = int(os.getenv("ADAPTIVE_LLM_MAX_UPGRADES", "50"))

# ==================== LLM 运行级熔断 + 批量抢救配置（grill 定稿） ====================
# 连续 N 次 LLM 调用失败（重试耗尽口径）→ 本 run 熔断：LLM 入口快速失败，
# 确定性链路全速继续（zztzmjg 实测：半死端点每页 30s+ 超时重试拖垮 BFS 主链路）
LLM_BREAKER_THRESHOLD = int(os.getenv("LLM_BREAKER_THRESHOLD", "3"))
# Evaluate 阶段批量抢救：最多为多少个 URL 模板调 LLM 定位（一次定位泛化整个栏目）
RESCUE_MAX_TEMPLATES = int(os.getenv("RESCUE_MAX_TEMPLATES", "6"))
# 批量抢救单 run 最多处理多少候选页（预算兜底，防大站雪崩）
RESCUE_MAX_PAGES = int(os.getenv("RESCUE_MAX_PAGES", "60"))

# ==================== Phase 2: HITL 人工介入配置 ====================
HITL_BATCH_SAVE_THRESHOLD = int(os.getenv("HITL_BATCH_SAVE_THRESHOLD", "1000"))  # 单次保存超过此数量触发中断
HITL_TOKEN_COST_THRESHOLD = float(os.getenv("HITL_TOKEN_COST_THRESHOLD", "10.0"))  # Token 累计费用超过 $10 触发中断
# DeepSeek 价格 (RMB per 1M tokens)
DEEPSEEK_PRICE_INPUT_PER_1M = float(os.getenv("DEEPSEEK_PRICE_INPUT", "1.0"))
DEEPSEEK_PRICE_OUTPUT_PER_1M = float(os.getenv("DEEPSEEK_PRICE_OUTPUT", "2.0"))
