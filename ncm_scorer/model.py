"""机器学习层：以「新歌榜最佳名次是否进入前 N」为标签训练流行度分类器.

设计要点：
- 旧标签「first_seen 后 horizon 天内进过榜」对「从新歌榜发现的歌」恒为 1，无法训练；
- 新标签：上榜且最佳名次 <= rank_cutoff 为正，未上榜（歌手邻域）或榜尾为负；
- 特征只用歌曲入库首日的快照（早期特征），杜绝标签泄漏；
- 数据不足（正样本 < 10）时拒绝训练并提示继续跑 daily.py 攒数据。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .features import FEATURE_NAMES, build_features

log = logging.getLogger(__name__)

MODEL_META = {"algo": "sklearn.GradientBoostingClassifier", "version": "gbc-v1"}


class ModelNotReady(RuntimeError):
    pass


def _load_sklearn():
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score
        return GradientBoostingClassifier, train_test_split, roc_auc_score
    except ImportError as e:  # pragma: no cover
        raise ModelNotReady(
            "需要 scikit-learn：pip install scikit-learn"
        ) from e


def build_labels(store, horizon_days: float = 14.0,
                 rank_cutoff: int = 20) -> Dict[int, int]:
    """对每首已跟踪歌曲打标签.

    正样本：上过新歌榜且历史最佳名次 <= rank_cutoff。
    负样本：从未上榜（歌手邻域发现）或上榜但名次更靠后。
    horizon_days 保留兼容，当前不参与判定（单日榜无法还原「发布后 N 天内首次上榜」）。
    """
    del horizon_days  # 接口保留，待有跨日榜史后再按发行窗口过滤
    songs = store.conn.execute("SELECT song_id FROM songs").fetchall()
    labels: Dict[int, int] = {}
    for s in songs:
        best = store.best_chart_rank(s["song_id"])
        labels[s["song_id"]] = 1 if best is not None and best <= rank_cutoff else 0
    return labels


def _first_day_features(store, song_id: int) -> Optional[Dict[str, float]]:
    """取该歌曲入库后 48h 内最早一次快照当时的特征（防泄漏：用早期视图）."""
    song = store.get_song(song_id)
    if song is None:
        return None
    snaps = store.snapshots(song_id)
    if not snaps:
        return None
    first = snaps[0]
    if first["comments_total"] is None:
        return None
    # 以首次快照时刻为 now 重建特征（增速恒为 0，属正常）
    feats = build_features(store, song_id, now=float(first["ts"]))
    feats["comments_total_log"] = _log1p(first["comments_total"])
    # build_features already uses the first snapshot and density per day.
    return feats


def _log1p(x: float) -> float:
    import math
    return math.log1p(max(float(x or 0), 0.0))


def _age_hours_at(publish_time_ms: Optional[int], at_ts: float) -> float:
    if not publish_time_ms:
        return 24.0 * 30
    return max(min((at_ts - publish_time_ms / 1000.0) / 3600.0, 24.0 * 90), 1.0)


def train(store, horizon_days: float = 14.0, model_path: str = "model.pkl"
          ) -> Dict[str, Any]:
    """训练分类器，模型以 pickle 序列化到 model_path，元信息写到同名 .meta.json."""
    GradientBoostingClassifier, train_test_split, roc_auc_score = _load_sklearn()
    labels = build_labels(store, horizon_days)
    X: List[List[float]] = []
    y: List[int] = []
    sid_kept: List[int] = []
    for sid, lab in labels.items():
        feats = _first_day_features(store, sid)
        if feats is None:
            continue
        X.append([feats[k] for k in FEATURE_NAMES])
        y.append(lab)
        sid_kept.append(sid)

    n_pos = sum(y)
    n_negative = len(y) - n_pos
    if len(y) < 40 or n_pos < 10 or n_negative < 10:
        raise ModelNotReady(
            f"样本不足：总 {len(y)} 首 / 正样本 {n_pos} 首 / 负样本 {n_negative} 首（需 >=40/10/10）。"
            "继续每日运行 daily.py 攒 2-3 周数据后再训练。"
        )

    x_train, x_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, stratify=y, random_state=42
    )
    clf = GradientBoostingClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42
    )
    clf.fit(x_train, y_train)
    auc = float("nan")
    try:
        if len(set(y_test)) == 2:
            auc = float(roc_auc_score(y_test, clf.predict_proba(x_test)[:, 1]))
    except Exception:  # pragma: no cover
        pass

    import pickle
    blob = pickle.dumps(clf)
    with open(model_path, "wb") as f:
        f.write(blob)
    meta = {
        **MODEL_META,
        "feature_names": FEATURE_NAMES,
        "n_samples": len(y),
        "n_positive": n_pos,
        "n_negative": n_negative,
        "auc": None if auc != auc else round(auc, 4),
        "horizon_days": horizon_days,
        "trained_at": int(time.time()),
        "model_path": model_path,
    }
    with open(model_path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log.info("trained: %s", meta)
    return meta


def load_model(model_path: str = "model.pkl"):
    """加载已训练的模型对象；文件不存在时抛 ModelNotReady."""
    import pickle
    if not os.path.exists(model_path):
        raise ModelNotReady(f"模型文件不存在：{model_path}，先运行 train")
    with open(model_path, "rb") as f:
        return pickle.load(f)


def predict_proba(features: Dict[str, float], model_path: str = "model.pkl"
                  ) -> float:
    """读取已训练模型，输出该歌「进入新歌榜」的概率 [0,1]."""
    clf = load_model(model_path)
    x = [[features[k] for k in FEATURE_NAMES]]
    return float(clf.predict_proba(x)[0][1])
