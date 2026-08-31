# 量化指标报告（离线生成）

- 生成时间: 2026-08-31 11:31:08
- 全部指标由本地已落盘产物离线计算，**不联网、不调用 LLM**，可复现。

## 1. Golden Set 离线评估（P/R/F1）

Golden 清单共 3 站，其中已有落盘证据、可出指标 1 站（其余站点的爬取可在联网时补跑）。

| 站点 | 保存量 | 期望 | 关键词 | P | R | F1 | 栏目发现率 | 判定 |
|------|-------:|-----:|:-----:|--:|--:|---:|:--:|:--:|
| hnbn666 | 10 | 6 | ✓ | 0.60 | 1.00 | 0.75 | - | PASS |

## 2. 实地运行统计（真实站点）

共 8 个真实站点，累计落盘 236 个 HTML 页面。

| 域名 | 保存 HTML | 栏目数 | 轨迹数 |
|------|---------:|-------:|-------:|
| www.clypg.com.cn | 66 | 30 | 0 |
| www.cqht.cn | 19 | 7 | 0 |
| www.dfgycrisp.com | 72 | 11 | 0 |
| www.hnbn666.cn | 10 | 5 | 0 |
| www.huinenggroup.com | 11 | 10 | 0 |
| www.jstcba.cn | 24 | 24 | 0 |
| www.sanzhigua.com | 11 | 7 | 0 |
| www.zsyllh.cn | 23 | 5 | 0 |

## 3. LLM 评估循环证据（调整前 vs 调整后）

共 0 条运行轨迹，其中 0 次运行触发了'评估→调整→重抓'闭环，累计 0 次配置调整。

| 站点 | 轮次 | 调整次数 | saved 变化 | 评分变化 | 规则生成 | 接管 |
|------|-----:|-------:|:--:|:--:|:--:|:--:|

> 说明：Token 成本由 `agents/budget.TrackedLLM` 在**运行时内存**记账（每次 run_crawler 汇总打印 `💰 Token预算`），进程退出后无法回溯历史数值；联网重跑 `tools/golden_check.py hnbn666` 即可实时导出当次成本。

## 4. 复现命令

```bash
python tools/golden_check.py hnbn666 --offline   # 单站 golden 离线复核
python tools/golden_check.py --list               # 查看 golden 清单
python tools/gen_metrics_report.py                 # 重新生成本报告
```