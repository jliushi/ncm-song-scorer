# ncm-scorer · 网易云音乐新歌自动评分系统

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

每天自动盯网易云**新歌榜**，给每首新歌打一个 0-100 的**研究排序分**。全开源，启发式冷启动；积累数据后可手动训练机器学习模型。评分不代表经过校准的未来走红概率。

线上榜：https://jliushi.github.io/ncm-song-scorer/

> 开工文档与完整设计见 [KICKOFF.md](KICKOFF.md)，端点实测记录见 [docs/API_ENDPOINTS.md](docs/API_ENDPOINTS.md)。

## 工作原理

```
新歌榜(每日100首) ──▶ 入库跟踪 ──▶ 歌手邻域近作(未上榜负样本)
                                      │
每日快照(评论总数/热度pop) ──▶ 特征工程 ──▶ 打分(heuristic-v2 或 ML)
                                      │
                              排名表 / GitHub Pages
```

**启发式分数 heuristic-v2**（冷启动即可用）：

| 分项 | 权重 | 说明 |
|---|---|---|
| 早期讨论密度 | 36% | 评论总数 ÷ 发行天数（log 压缩） |
| 平台热度值 | 22% | 官方 pop 字段（0-100） |
| 评论增速 | 18% | 相邻快照差分得出的每小时评论增量 |
| 歌手资历 | 12% | 专辑数 + 单曲数（log 压缩） |
| 歌手上榜热 | 12% | 主歌手近 90 天其他歌曲的上榜天数（同日不计） |

歌名判定为 Live / 现场 / 翻唱时，总分再乘 0.78，避免综艺回放压过原创新歌。

**ML 分数**：以「历史新歌榜最佳名次 ≤ 20」为标签训练 GradientBoosting。评论、热度及增速只读取首日时点以前的快照；训练和日常评分使用相同的按天讨论密度单位。歌手资历元数据尚无历史快照，标签也是历史最佳名次，因此当前模型属于回顾性分类，仍需独立时间窗口验证预测能力。总样本 ≥40、正负样本各 ≥10 后才能训练。

## 快速开始

```bash
git clone https://github.com/jliushi/ncm-song-scorer.git
cd ncm-song-scorer
uv venv && uv pip install -r requirements.txt    # 或 python -m venv .venv + pip

python -m ncm_scorer daily      # 拉榜 + 邻域发现 + 快照 + 打分（每天跑一次）
python -m ncm_scorer top -n 20  # 查看当前新歌排名
python scripts/build_site.py    # 生成本地排名页
```

## CLI 命令

| 命令 | 作用 |
|---|---|
| `daily` | 每日例行：拉榜、邻域发现、补详情、拍快照、打分 |
| `charts` | 仅拉取新歌榜入库 |
| `snapshot` | 对已跟踪歌曲拍互动快照 |
| `score` | 重算启发式分数 |
| `top -n N` | 输出当前 Top N 排名 |
| `train` | 训练 ML 模型（样本 ≥40 / 正负样本各 ≥10 才放行） |

全局参数 `--db` 指定 SQLite 路径（默认 `ncm_scorer.db`）。

## 每日定时

页面先保留全部近 30 天发行的评分候选，再按近 7 天、是否包含 Live、歌名/歌手搜索筛选各自的 Top 50。筛选后重排编号并显示匹配数量。发行日期缺失、未来发行及超窗歌曲不混入新歌榜。

采集时间来自有效评论快照，重新构建页面或重算分数不会刷新这个时间；超过 48 小时会提示数据陈旧。单首接口失败会保留旧快照；整次采集失败或无法恢复数据库会停止发布。ML 结果过期或未覆盖当前候选时使用完整启发式榜。

试听支持切歌、重试、关闭和网易云原页入口；请求超时、会员/版权限制会显示反馈。Cloudflare 部署需要仓库 Secrets `CLOUDFLARE_API_TOKEN` 和 `CLOUDFLARE_ACCOUNT_ID`，未配置时工作流会明确跳过部署，不能把跳过当成代理已更新。

验证命令：`python -m unittest discover -s tests -q` 和 `node --test tests/worker.test.mjs`。提交源码会通过检查后，用已有数据库重建页面；定时及手动运行仍执行真实采集。

线上已由 GitHub Actions 在**北京时间 09:30**自动采集，并把排名页发布到 GitHub Pages。数据库落在私有仓 `ncm-scorer-data`。

本机补跑：

**Windows 任务计划：**

```bat
schtasks /create /tn ncm-scorer-daily ^
  /tr "C:\path\to\ncm-song-scorer\.venv\Scripts\python.exe C:\path\to\ncm-song-scorer\scripts\daily.py" ^
  /sc daily /st 09:00
```

**Linux cron：** `0 9 * * * cd /path/to/ncm-song-scorer && .venv/bin/python scripts/daily.py`

## 项目结构

```
ncm_scorer/          # 核心包
  api.py             # 网易云明文 API 直连客户端（已实测）
  storage.py         # SQLite：songs/snapshots/charts/scores
  features.py        # 9 个核心特征（log1p 压缩、防泄漏）
  scoring.py         # 启发式打分 heuristic-v2
  model.py           # GBDT 训练/推理 gbc-v1
  pipeline.py        # daily 业务编排
  audio_features.py  # 可选 librosa 音频特征
  cli.py             # 命令行入口
scripts/daily.py     # 定时任务脚本
scripts/build_site.py
tests/               # 离线单测（pytest，不联网）
```

## 免责声明

- 仅供**个人学习与研究**。数据归网易云音乐所有，请勿二次分发原始数据、勿对接口施压（内置 1 req/s 限速请保留）。
- 分数是排序参考，不是爆款保证——学术研究表明早期数据预测爆款存在天花板。
- 使用本项目产生的任何后果由使用者自行承担。

## License

[MIT](LICENSE)
