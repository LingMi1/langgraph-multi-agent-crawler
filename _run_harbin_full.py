# -*- coding: utf-8 -*-
"""全站重启脚本：注入 config.json 配置，运行 LangGraph MA 爬取 harbin（全站，新提速代码）"""
import os
import json
import sys
import io

# 日志同时输出到文件，方便事后查看
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_harbin_full.log")
_logf = open(_log_path, "a", encoding="utf-8")

def _log(msg: str):
    print(msg)
    try:
        _logf.write(str(msg) + "\n")
        _logf.flush()
    except Exception:
        pass

# 1. 加载 config.json 的 API 配置（GUI 同款注入逻辑）
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
          "r", encoding="utf-8") as f:
    _j = json.load(f)

_api_key = _j.get("api_key", "")
_base_url = _j.get("base_url", "")
_model = _j.get("model_name", "")

os.environ["DEEPSEEK_API_KEY"] = _api_key
os.environ["DEEPSEEK_BASE_URL"] = _base_url
os.environ["DEEPSEEK_MODEL"] = _model

# 2. 全站范围（不限制页面数，深度用默认 5）
os.environ.pop("MAX_PAGES", None)
os.environ.pop("MAX_DEPTH", None)

# 3. 注入 config 模块
import config as _cfg
_cfg.DEEPSEEK_API_KEY = _api_key
_cfg.DEEPSEEK_BASE_URL = _base_url
_cfg.DEEPSEEK_MODEL = _model

# 4. 强制重置 LLM 单例（确保用新 key/base_url/model）
from graph import nodes as _gn
_gn.reset_llm()
from agents import extractor as _ext
_ext.reset_llm_client()

# 5. 运行 LangGraph MA 模式
from main import run_langgraph_crawler

if __name__ == "__main__":
    _log("[START] harbin 全站重启爬取（新提速代码）")
    run_langgraph_crawler(
        "https://www.harbin-electric.com/",
        concurrency=5,
        log_callback=_log,
        reset_memory=True,
    )
    _log("[DONE] harbin 全站爬取结束")
    _logf.close()
