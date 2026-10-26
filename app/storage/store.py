"""SQLite 存储层：结构化列用于过滤，完整对象以 JSON 保存。

设计取舍：骨架阶段不引 ORM，保持零额外依赖与低内存占用；
需要换 PostgreSQL 时只需替换本模块的 Store 实现。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models.alert import Alert
from app.models.cluster import Cluster
from app.models.diagnosis import DiagnosisReport
from app.models.events import AnomalyEvent
from app.models.knowledge import Experience, KnowledgeStats
from app.utils.clock import iso_utc, parse_iso, utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    cluster TEXT NOT NULL,
    namespace TEXT NOT NULL,
    target TEXT NOT NULL,
    metric TEXT NOT NULL,
    group_key TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_group ON alerts(group_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, severity);

CREATE TABLE IF NOT EXISTS diagnoses (
    id TEXT PRIMARY KEY,
    anomaly_id TEXT NOT NULL,
    status TEXT NOT NULL,
    cluster TEXT NOT NULL,
    namespace TEXT NOT NULL,
    target TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diag_created ON diagnoses(created_at DESC);

CREATE TABLE IF NOT EXISTS anomalies (
    id TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    namespace TEXT NOT NULL,
    target TEXT NOT NULL,
    metric TEXT NOT NULL,
    severity TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ano_created ON anomalies(created_at DESC);

CREATE TABLE IF NOT EXISTS experiences (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences(status);

CREATE TABLE IF NOT EXISTS clusters (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    egress INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Store:
    """线程安全的轻量仓储。每次操作取独立连接，避免长事务。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_schema()

    # ---------- 基础设施 ----------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def _dump(obj: Any) -> str:
        if hasattr(obj, "model_dump_json"):
            return obj.model_dump_json()
        return json.dumps(obj, ensure_ascii=False, default=str)

    # ---------- 异常事件 ----------
    def save_anomaly(self, anomaly: AnomalyEvent) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO anomalies"
                "(id, cluster, namespace, target, metric, severity, rule_id, created_at, data)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    anomaly.id,
                    anomaly.cluster,
                    anomaly.namespace,
                    anomaly.target,
                    anomaly.metric,
                    anomaly.severity.value,
                    anomaly.rule_id,
                    iso_utc(anomaly.created_at),
                    self._dump(anomaly),
                ),
            )

    def list_anomalies(self, limit: int = 50) -> list[AnomalyEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT data FROM anomalies ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [AnomalyEvent.model_validate_json(r["data"]) for r in rows]

    # ---------- 诊断 ----------
    def save_diagnosis(self, report: DiagnosisReport) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO diagnoses"
                "(id, anomaly_id, status, cluster, namespace, target, confidence,"
                " duration_ms, created_at, data) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    report.id,
                    report.anomaly_id,
                    report.status.value,
                    report.cluster,
                    report.namespace,
                    report.target,
                    report.confidence,
                    report.duration_ms,
                    iso_utc(report.created_at),
                    self._dump(report),
                ),
            )

    def get_diagnosis(self, diagnosis_id: str) -> DiagnosisReport | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM diagnoses WHERE id = ?", (diagnosis_id,)
            ).fetchone()
        return DiagnosisReport.model_validate_json(row["data"]) if row else None

    def list_diagnoses(
        self, limit: int = 20, cluster: str | None = None
    ) -> list[DiagnosisReport]:
        sql = "SELECT data FROM diagnoses"
        params: list[Any] = []
        if cluster:
            sql += " WHERE cluster = ?"
            params.append(cluster)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [DiagnosisReport.model_validate_json(r["data"]) for r in rows]

    # ---------- 告警 ----------
    def save_alert(self, alert: Alert) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO alerts"
                "(id, severity, status, cluster, namespace, target, metric, group_key,"
                " count, created_at, last_seen_at, data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert.id,
                    alert.severity.value,
                    alert.status.value,
                    alert.cluster,
                    alert.namespace,
                    alert.target,
                    alert.metric,
                    alert.group_key,
                    alert.count,
                    iso_utc(alert.created_at),
                    iso_utc(alert.last_seen_at),
                    self._dump(alert),
                ),
            )

    def get_alert(self, alert_id: str) -> Alert | None:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return Alert.model_validate_json(row["data"]) if row else None

    def find_alert_in_group(self, group_key: str, since: datetime) -> Alert | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM alerts WHERE group_key = ? AND created_at >= ?"
                " AND status != 'resolved' ORDER BY created_at DESC LIMIT 1",
                (group_key, iso_utc(since)),
            ).fetchone()
        return Alert.model_validate_json(row["data"]) if row else None

    def list_alerts(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        severity: str | None = None,
        cluster: str | None = None,
    ) -> tuple[list[Alert], int]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if cluster:
            where.append("cluster = ?")
            params.append(cluster)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) c FROM alerts{clause}", params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT data FROM alerts{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [Alert.model_validate_json(r["data"]) for r in rows], int(total)

    def count_alerts_by_severity(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT severity, COUNT(*) c FROM alerts WHERE status != 'resolved'"
                " GROUP BY severity"
            ).fetchall()
        return {r["severity"]: int(r["c"]) for r in rows}

    # ---------- 经验知识库 ----------
    def save_experience(self, exp: Experience) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiences"
                "(id, status, title, confirmed_by, created_at, data) VALUES(?,?,?,?,?,?)",
                (
                    exp.id,
                    exp.status.value,
                    exp.title,
                    exp.confirmed_by,
                    iso_utc(exp.created_at),
                    self._dump(exp),
                ),
            )

    def get_experience(self, exp_id: str) -> Experience | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM experiences WHERE id = ?", (exp_id,)
            ).fetchone()
        return Experience.model_validate_json(row["data"]) if row else None

    def list_experiences(
        self, status: str | None = None, limit: int = 100
    ) -> list[Experience]:
        sql, params = "SELECT data FROM experiences", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Experience.model_validate_json(r["data"]) for r in rows]

    def knowledge_stats(self, backend: str) -> KnowledgeStats:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM experiences GROUP BY status"
            ).fetchall()
        counts = {r["status"]: int(r["c"]) for r in rows}
        return KnowledgeStats(
            total=sum(counts.values()),
            draft=counts.get("draft", 0),
            confirmed=counts.get("confirmed", 0),
            rolled_back=counts.get("rolled_back", 0),
            backend=backend,
        )

    # ---------- 集群 ----------
    def save_cluster(self, cluster: Cluster) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO clusters(id, name, created_at, data) VALUES(?,?,?,?)",
                (cluster.id, cluster.name, iso_utc(cluster.created_at), self._dump(cluster)),
            )

    def get_cluster_by_name(self, name: str) -> Cluster | None:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM clusters WHERE name = ?", (name,)).fetchone()
        return Cluster.model_validate_json(row["data"]) if row else None

    def list_clusters(self) -> list[Cluster]:
        with self.connect() as conn:
            rows = conn.execute("SELECT data FROM clusters ORDER BY created_at").fetchall()
        return [Cluster.model_validate_json(r["data"]) for r in rows]

    # ---------- 审计 ----------
    def append_audit(
        self,
        action: str,
        detail: dict[str, Any],
        actor: str = "system",
        target: str = "",
        egress: bool = False,
    ) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, action, actor, target, egress, detail)"
                " VALUES(?,?,?,?,?,?)",
                (
                    iso_utc(utc_now()),
                    action,
                    actor,
                    target,
                    int(egress),
                    json.dumps(detail, ensure_ascii=False, default=str),
                ),
            )

    def list_audit(self, limit: int = 50, egress_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit"
        if egress_only:
            sql += " WHERE egress = 1"
        sql += " ORDER BY seq DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"] or "{}")
            item["egress"] = bool(item["egress"])
            out.append(item)
        return out

    # ---------- KV ----------
    def kv_set(self, key: str, value: Any) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv(k, v) VALUES(?,?)",
                (key, json.dumps(value, ensure_ascii=False, default=str)),
            )

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    # ---------- 保留策略 ----------
    def purge(self, retention_days: int) -> dict[str, int]:
        """清理过期告警/诊断/异常/审计；经验条目不自动删除。"""
        cutoff = iso_utc(utc_now() - timedelta(days=retention_days))
        removed: dict[str, int] = {}
        with self._lock, self.connect() as conn:
            for table in ("alerts", "diagnoses", "anomalies", "audit"):
                column = "ts" if table == "audit" else "created_at"
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                removed[table] = cur.rowcount
        if any(removed.values()):
            logger.info("保留期清理完成：%s", removed)
        return removed

    # ---------- 概览 ----------
    def overview(self) -> dict[str, Any]:
        with self.connect() as conn:
            alerts = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) c FROM alerts WHERE status != 'resolved'"
            ).fetchone()["c"]
            diags = conn.execute("SELECT COUNT(*) c FROM diagnoses").fetchone()["c"]
            exps = conn.execute(
                "SELECT COUNT(*) c FROM experiences WHERE status = 'confirmed'"
            ).fetchone()["c"]
            clusters = conn.execute("SELECT COUNT(*) c FROM clusters").fetchone()["c"]
        return {
            "alerts_total": int(alerts),
            "alerts_active": int(active),
            "diagnoses_total": int(diags),
            "experiences_confirmed": int(exps),
            "clusters_total": int(clusters),
            "generated_at": iso_utc(utc_now()),
        }


def parse_datetime(value: str) -> datetime:
    if isinstance(value, datetime):  # pragma: no cover - 兜底
        return value
    return parse_iso(value)


def utc_stamp(value: datetime | None = None) -> str:
    return iso_utc(value or datetime.now(timezone.utc))
