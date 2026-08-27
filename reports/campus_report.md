# 校招证据报告（离线生成）

- 生成时间: 2026-08-27 13:25:59
- 全部指标由本地已落盘产物离线计算，**不联网、不调用 LLM**，可复现。

## 1. Golden Set 离线评估（P/R/F1）

Golden 清单共 3 站，其中已有落盘证据、可出指标 1 站（其余站点的爬取可在联网时补跑）。

| 站点 | 保存量 | 期望 | 关键词 | P | R | F1 | 栏目发现率 | 判定 |
|------|-------:|-----:|:-----:|--:|--:|---:|:--:|:--:|
| hnbn666 | 6 | 6 | ✓ | 1.00 | 1.00 | 1.00 | - | PASS |

## 2. 实地运行统计（真实站点）

共 6 个真实站点，累计落盘 240 个 HTML 页面。

| 域名 | 保存 HTML | 栏目数 | 轨迹数 |
|------|---------:|-------:|-------:|
| feiyima.com.cn | 5 | 3 | 1 |
| www.cqht.cn | 18 | 6 | 2 |
| www.hnbn666.cn | 6 | 2 | 1 |
| www.xnjzgc.cn | 98 | 5 | 1 |
| www.zsyllh.cn | 29 | 5 | 1 |
| www.zztzmjg.com | 84 | 20 | 1 |

## 3. LLM 评估循环证据（调整前 vs 调整后）

共 7 条运行轨迹，其中 4 次运行触发了'评估→调整→重抓'闭环，累计 12 次配置调整。

| 站点 | 轮次 | 调整次数 | saved 变化 | 评分变化 | 规则生成 | 接管 |
|------|-----:|-------:|:--:|:--:|:--:|:--:|
| http://www.hnbn666.cn/ | 13 | 3 | 1→6 | 0.62→0.45 | ✓ | decision=retry |
| http://www.xnjzgc.cn/ | 75 | 3 | 1→98 | 0.6→0.85 | ✓ | - |
| https://www.zsyllh.cn/ | 30 | 3 | 4→29 | 0.76→0.5 | ✓ | - |
| http://www.zztzmjg.com/ | 83 | 3 | 3→84 | 0.5→0.45 | ✓ | - |

> 说明：Token 成本由 `agents/budget.TrackedLLM` 在**运行时内存**记账（每次 run_crawler 汇总打印 `💰 Token预算`），进程退出后无法回溯历史数值；联网重跑 `tools/golden_check.py hnbn666` 即可实时导出当次成本。

## 4. 复现命令

```bash
python tools/golden_check.py hnbn666 --offline   # 单站 golden 离线复核
python tools/golden_check.py --list               # 查看 golden 清单
python tools/gen_campus_report.py                 # 重新生成本报告
```