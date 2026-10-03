"""把当前排名生成单文件静态页 index.html（供 GitHub Pages 发布）.

用法: python scripts/build_site.py [--db ncm_scorer.db] [--out site/index.html] [--top 50]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm_scorer.api import is_vip_fee  # noqa: E402
from ncm_scorer.features import title_flags  # noqa: E402
from ncm_scorer.storage import Store  # noqa: E402

PARTS = (
    ("density", "讨论密度"),
    ("pop", "平台热度"),
    ("velocity", "评论增速"),
    ("artist", "歌手资历"),
    ("artist_heat", "上榜热"),
)

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ncm-scorer · 网易云新歌爆款潜力榜</title>
<meta name="description" content="每日采集的新歌研究榜，提供评分构成、发行筛选、歌曲搜索和官方试听入口。">
<link rel="icon" href="data:,">
<style>
  :root {{
    color-scheme: dark;
    --bg: #0b0b0d;
    --card: #141416;
    --ink: #f4f4f5;
    --muted: #8d8d93;
    --line: #26262b;
    --accent: #ec4141;
    --gold: #f0c36a;
    --vip: #f0a14a;
  }}
  * {{ box-sizing: border-box; }}
  [hidden] {{ display: none !important; }}
  body {{
    margin: 0;
    min-height: 100vh;
    font-family: "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
    background:
      radial-gradient(900px 420px at 10% -10%, #3a121480, transparent 55%),
      var(--bg);
    color: var(--ink);
    line-height: 1.5;
  }}
  .wrap {{
    width: min(980px, calc(100% - 28px));
    margin: 0 auto;
    padding: 36px 0 80px;
  }}
  .hero {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    padding-bottom: 22px;
    border-bottom: 1px solid var(--line);
  }}
  .kicker {{
    margin: 0 0 8px;
    color: var(--accent);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .16em;
  }}
  h1 {{
    margin: 0;
    font-size: clamp(1.8rem, 5vw, 2.6rem);
    font-weight: 780;
    letter-spacing: -.04em;
    line-height: 1.1;
  }}
  .meta {{
    margin: 0;
    max-width: 280px;
    color: var(--muted);
    font-size: 13px;
    text-align: right;
  }}
  .toolbar {{
    display: flex;
    align-items: center;
    gap: 14px 20px;
    flex-wrap: wrap;
    margin: 18px 0 16px;
  }}
  .filter-group {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .filter-label {{ color: var(--muted); font-size: 12px; }}
  .filters {{
    display: flex;
    padding: 3px;
    border-radius: 999px;
    background: #1c1c20;
  }}
  .filters button {{
    border: 0;
    background: transparent;
    color: var(--muted);
    border-radius: 999px;
    padding: 6px 12px;
    cursor: pointer;
    font-size: 13px;
  }}
  .filters button.on {{
    background: var(--accent);
    color: #fff;
  }}
  #player-box {{
    position: sticky;
    top: 8px;
    z-index: 5;
    margin-bottom: 14px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: #1a1a1e;
  }}
  #player-box audio {{ width: 100%; display: block; }}
  .play-title {{ font-weight: 700; margin-bottom: 8px; }}
  .play-hint {{ font-size: 13px; color: var(--muted); margin-top: 8px; }}
  .board {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 16px;
    overflow: hidden;
  }}
  .board-head, .row {{
    display: grid;
    grid-template-columns: 48px 64px minmax(0, 1.5fr) minmax(0, .9fr) 104px 44px;
    gap: 10px;
    align-items: center;
    padding: 13px 16px;
  }}
  .board-head {{
    color: #6f6f75;
    font-size: 12px;
    letter-spacing: .04em;
    border-bottom: 1px solid var(--line);
    background: #18181b;
  }}
  .song {{ border-bottom: 1px solid var(--line); cursor: pointer; }}
  .song:last-child {{ border-bottom: 0; }}
  .song:hover {{ background: #1c1c20; }}
  .song.hidden {{ display: none; }}
  .c-rank, .c-score {{ font-variant-numeric: tabular-nums; }}
  .c-rank {{
    color: #6f6f75;
    font-size: 18px;
    font-weight: 760;
  }}
  .c-score {{ font-size: 18px; font-weight: 780; }}
  .top3 .c-rank {{ color: var(--accent); }}
  .top3 .c-score {{ color: var(--gold); }}
  .name {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    font-weight: 700;
  }}
  .name a {{ color: inherit; text-decoration: none; }}
  .name a:hover {{ color: var(--accent); }}
  .artists, .sub {{ color: var(--muted); font-size: 13px; }}
  .sub {{ display: none; margin-top: 4px; }}
  .c-date {{ color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
  .badge {{
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    border-radius: 4px;
    padding: 1px 6px;
    background: #2a2a30;
    color: #c8c8ce;
  }}
  .badge.vip {{ background: #3a2612; color: var(--vip); }}
  button.play {{
    width: 34px;
    height: 34px;
    border: 0;
    border-radius: 50%;
    background: var(--accent);
    color: #fff;
    cursor: pointer;
    font-size: 12px;
  }}
  button.play:hover {{ filter: brightness(1.12); }}
  .detail {{ padding: 0 16px 14px; }}
  .parts {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .parts span {{
    background: #1c1c20;
    border-radius: 8px;
    padding: 5px 8px;
    color: var(--muted);
    font-size: 12px;
  }}
  .parts b {{ color: var(--ink); margin-left: 6px; font-variant-numeric: tabular-nums; }}
  .foot {{
    color: var(--muted);
    font-size: 12px;
    margin-top: 22px;
    max-width: 68ch;
  }}
  a {{ color: #ff7a7a; }}
  button, input {{ font: inherit; }}
  button:focus-visible, a:focus-visible, input:focus-visible {{ outline: 2px solid var(--gold); outline-offset: 3px; }}
  .search {{ display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }}
  .search input {{ flex: 1 1 220px; min-width: 0; max-width: 440px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--ink); }}
  #result-count, .data-age {{ font-size: 12px; color: var(--muted); }}
  #freshness, #empty-state {{ padding: 14px; color: var(--gold); }}
  .details-toggle, .close-player, .retry-player {{ border: 1px solid var(--line); background: #252529; color: var(--ink); border-radius: 6px; padding: 5px 8px; cursor: pointer; }}
  .details-toggle {{ font-size: 11px; margin-top: 4px; }}
  .player-actions {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 10px; }}
  @media (max-width: 760px) {{
    .wrap {{ width: min(100% - 18px, 980px); padding-top: 20px; }}
    .hero {{ display: block; }}
    .meta {{ max-width: none; text-align: left; margin-top: 10px; }}
    .board-head, .c-score, .c-artists, .c-date {{ display: none; }}
    .row {{
      grid-template-columns: 36px minmax(0, 1fr) 38px;
      padding: 12px;
    }}
    .sub {{ display: block; }}
    .detail {{ padding-left: 12px; padding-right: 12px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div>
  <p class="kicker">ncm-scorer</p>
  <h1>新歌爆款潜力榜</h1>
  </div>
  <p class="meta">采集更新于 {updated} · {model} · 数据来自网易云音乐新歌榜及歌手近作（近 30 天发行），每日自动采集</p>
</header>
<p id="freshness" role="status" {freshness_hidden}>{freshness}</p>
<div class="toolbar" id="filters">
  <div class="filter-group" data-group="time">
    <span class="filter-label">发行</span>
    <div class="filters">
      <button type="button" data-time="month" class="on" aria-pressed="true">近 30 天</button>
      <button type="button" data-time="week" aria-pressed="false">近 7 天</button>
    </div>
  </div>
  <div class="filter-group" data-group="live">
    <span class="filter-label">类型</span>
    <div class="filters">
      <button type="button" data-live="all" class="on" aria-pressed="true">含 Live</button>
      <button type="button" data-live="studio" aria-pressed="false">不含 Live</button>
    </div>
  </div>
</div>
<div class="search"><label for="song-search">搜索歌曲 / 歌手</label><input id="song-search" type="search" placeholder="输入歌名或歌手" autocomplete="off"><span id="result-count" role="status"></span></div>
<div id="player-box" hidden>
  <div class="play-title"></div><audio controls preload="metadata"></audio>
  <div class="play-hint" role="status"></div>
  <div class="player-actions"><button type="button" class="retry-player">重试</button><a class="official" target="_blank" rel="noopener noreferrer">前往网易云</a><button type="button" class="close-player">关闭试听</button></div>
</div>
<div class="board">
  <div class="board-head">
    <span>#</span><span>分数</span><span>歌曲</span><span>歌手</span><span>发布</span><span></span>
  </div>
  {rows}
  <p id="empty-state" {empty_hidden}>没有符合条件的歌曲，请调整筛选或搜索条件。</p>
</div>
<noscript>启用 JavaScript 可使用搜索、筛选、分数明细和页内试听；歌曲链接仍可直接打开。</noscript>
<div class="foot">
  {foot}
  点 ▶ 在本页播放，点歌曲行可看分数构成。仅个人研究用途，数据归网易云音乐所有。
  分数用于研究排序，不代表未来走红概率。项目：<a href="https://github.com/jliushi/ncm-song-scorer">ncm-song-scorer</a>
</div>
</div>
<script type="application/json" id="ranking-config">{config}</script>
<script>{client_script}</script>
<script type="application/ld+json">{ldjson}</script>
</body>
</html>
"""


def _fmt_publish(ms) -> str:
    if not ms:
        return "—"
    # 网易云 publishTime 按北京时间日期，用 UTC 会错一天
    return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000 + 8 * 3600))


def _age_days(publish_ms) -> int:
    if not publish_ms:
        return 999
    return max(int((time.time() - int(publish_ms) / 1000.0) / 86400), 0)


def _parse_detail(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _pick_rows(store: Store, top_n: int, now=None):
    now = time.time() if now is None else now
    # Include every eligible candidate; each filter applies its own Top N in the browser.
    heuristic = store.latest_scores('heuristic-v2', limit=None, max_age_days=30, now=now)
    choices = (
        ("gbc-v1", "ML 模型 gbc-v1（历史前 20 分类得分）",
         "分数 0-100，机器学习模型 gbc-v1（标签 = 历史新歌榜最佳名次 ≤ 20，未经前瞻校准）。"),
        ("heuristic-v2", "启发式 heuristic-v2（Live 降权 + 歌手上榜热）",
         "分数 0-100，启发式 heuristic-v2（讨论密度 36% + 平台热度 22% + 评论增速 18% + 歌手资历 12% + 上榜热 12%；Live/翻唱 ×0.78）。"),
        ("heuristic-v1", "启发式 heuristic-v1（未训练，冷启动基线）",
         "分数 0-100，启发式模型 heuristic-v1（讨论密度 40% + 平台热度 25% + 评论增速 20% + 歌手资历 15%）。"),
    )
    for version, label, foot in choices:
        rows = heuristic if version == 'heuristic-v2' else store.latest_scores(model_version=version, limit=None, max_age_days=30, now=now)
        if version == 'gbc-v1' and heuristic:
            by_id = {r['song_id']: r for r in rows}
            if not all(r['song_id'] in by_id and by_id[r['song_id']]['ts'] >= r['ts'] - 3600 for r in heuristic):
                continue  # Incomplete or stale ML output must not hide current heuristic candidates.
        if rows:
            return rows, label, foot
    return [], "暂无符合发行窗口的评分", "当前没有近 30 天发行且有评分记录的歌曲。"


DEFAULT_PLAY_API = "https://ncm-scorer-play.2383566697.workers.dev/"


def build(db_path: str, out_path: str, top_n: int = 50,
          play_api: str = DEFAULT_PLAY_API) -> int:
    if top_n < 1:
        raise ValueError('top must be positive')
    if not os.path.isfile(db_path):
        raise ValueError('数据库文件不存在，先运行 daily')
    store = Store(db_path)
    try:
        rows, model_label, foot = _pick_rows(store, top_n)
    finally:
        store.close()

    cards = []
    for i, r in enumerate(rows, start=1):
        song_id = int(r.get("song_id") or 0)
        name = str(r.get("name") or "")
        artists = html.escape(str(r.get("artists") or ""))
        score = float(r.get("score") or 0)
        published = html.escape(_fmt_publish(r.get("publish_time")))
        flags = title_flags(name)
        is_live = flags["is_live"] or bool(_parse_detail(r.get("detail")).get("is_live"))
        age = _age_days(r.get("publish_time"))
        fee = r.get("fee")
        is_vip = is_vip_fee(fee)
        badges = []
        if is_live:
            badges.append('<span class="badge">Live</span>')
        if is_vip:
            badges.append('<span class="badge vip">VIP</span>')
        badge = "".join(badges)
        cls = "song top3" if i <= 3 else "song"
        if i > top_n:
            cls += ' hidden'
        play_btn = (
            f'<button class="play" data-id="{song_id}" data-fee="{int(fee or 0)}" '
            f'data-name="{html.escape(name)}" '
            f'aria-label="播放 {html.escape(name)}" aria-pressed="false" title="本页播放">▶</button>'
        )
        detail = _parse_detail(r.get("detail"))
        parts = []
        for key, label in PARTS:
            if key in detail:
                parts.append(f"<span>{label}<b>{float(detail[key]):.1f}</b></span>")
        if detail.get("is_live"):
            parts.append(f"<span>Live 降权<b>×{detail.get('live_penalty', 0.78)}</b></span>")
        parts_html = "".join(parts) or "<span>暂无分项明细</span>"
        cards.append(
            f'<article class="{cls}" data-live="{1 if is_live else 0}" data-age="{age}" '
            f'data-published="{int(r.get("publish_time") or 0)}" data-search="{html.escape((name + " " + str(r.get("artists") or "")).lower())}">'
            f'<div class="row">'
            f'<div class="c-rank">{i}</div>'
            f'<div class="c-score">{score:.1f}</div>'
            f'<div class="c-song"><div class="name">'
            f'<a href="https://music.163.com/song?id={song_id}" target="_blank" '
            f'rel="noopener">{html.escape(name)}</a>{badge}</div>'
            f'<div class="sub">{artists} · {score:.1f} · 发布 {published}</div>'
            f'<button class="details-toggle" type="button" aria-expanded="false" aria-controls="detail-{song_id}">分数明细</button></div>'
            f'<div class="c-artists artists">{artists}</div>'
            f'<div class="c-date">{published}</div>'
            f'<div class="c-play">{play_btn}</div>'
            f'</div>'
            f'<div class="detail" id="detail-{song_id}" hidden><div class="parts">{parts_html}</div>'
            f'<p class="data-age">本曲采集：{time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["data_ts"])) if r.get("data_ts") else "未知"}</p></div>'
            f'</article>'
        )
    data_at = max((r.get('data_ts') or 0 for r in rows), default=0)
    updated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(data_at)) if data_at else '未知'
    stale = not data_at or time.time() - data_at > 48 * 3600
    def safe_json(value):
        return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    page = TEMPLATE.format(
        updated=updated,
        model=html.escape(model_label),
        config=safe_json({'playApi': play_api or '', 'top': top_n, 'dataAt': data_at}),
        client_script=Path(__file__).with_name('ranking.js').read_text(encoding='utf-8'),
        freshness_hidden='' if stale else 'hidden',
        freshness='数据已超过 48 小时未更新，当前展示上次采集结果。' if data_at else '暂无有效采集时间，请稍后查看更新。',
        empty_hidden='hidden' if rows else '',
        rows="\n".join(cards),
        foot=foot,
        ldjson=safe_json(
            {"name": "ncm-scorer ranking", "updated": updated,
             "top1": rows[0].get("name") if rows else None},
        ),
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"site written: {out_path} ({len(rows)} rows)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="ncm_scorer.db")
    parser.add_argument("--out", default="site/index.html")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--play-api", default=os.environ.get("PLAY_API", DEFAULT_PLAY_API),
                        help="官方播放地址代理（Cloudflare Worker）")
    args = parser.parse_args()
    return build(args.db, args.out, args.top, play_api=args.play_api)


if __name__ == "__main__":
    raise SystemExit(main())
