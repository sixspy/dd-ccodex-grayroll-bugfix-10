"""环境目录与时区 / 跨区资格。

正确性口径（甲路径，当班封死，两边不得混用）：
- 缺时区 ⇒ 可跨区正式标记为零且为假；
- 正式零增（eligible 0→1）在缺时区时一律拒写（409），正式区保持零；
- 不启用影子资格草案（乙路径不混用），无第三路折中；
- 资格写路径互斥锁 duel_env_tz_cross_region，争用段独立事务（锁行先落库再办业务）；
  并发双请求败方 409 + contention_marks 留痕，零正式改；
- 可跨区正式标记存 env_cross_region，写读均同事务联查 environments.timezone（跨表账实）；
  清除时区同事务把正式资格归零。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release

router = APIRouter(prefix="/api/envs", tags=["envs"])

DUEL_LOCK_KEY = "duel_env_tz_cross_region"
_TZ_RE = re.compile(r"^(UTC|[A-Za-z]+/[A-Za-z_\-]+(/[A-Za-z_\-]+)?)$")


class TimezoneIn(BaseModel):
    timezone: str = ""  # 空串 = 清除时区校准


class CrossRegionIn(BaseModel):
    eligible: bool


def _row_dict(row) -> dict:
    tz = (row["timezone"] or "").strip()
    official = int(row["cr_eligible"] or 0)
    return {
        "id": row["id"],
        "code": row["code"],
        "title": row["title"],
        "risk_level": row["risk_level"],
        "timezone": row["timezone"] or "",
        "tz_calibrated": bool(tz),
        "cross_region_official": official,
        # 生效资格 = 正式标记且时区在（缺时区恒假，甲路径读模型）
        "cross_region": bool(tz and official),
    }


_SELECT = """
    SELECT e.*, cr.eligible AS cr_eligible
    FROM environments e
    LEFT JOIN env_cross_region cr ON cr.env_id=e.id AND cr.official=1
"""


@router.get("")
def list_envs(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [_row_dict(r) for r in conn.execute(_SELECT + " ORDER BY e.risk_level, e.id").fetchall()]
    return {"items": rows}


@router.get("/{env_id}")
def get_env(env_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute(_SELECT + " WHERE e.id=?", (env_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="环境不存在")
    return _row_dict(row)


@router.put("/{env_id}/timezone")
def put_timezone(env_id: int, body: TimezoneIn, authorization: str | None = Header(default=None)):
    """设置或清除时区；清除时同事务把 env_cross_region 正式资格归零（账实同事务跨表）。"""
    user = current_user(authorization)
    require_writer(user)
    tz = (body.timezone or "").strip()
    if tz and not _TZ_RE.match(tz):
        raise HTTPException(400, detail="时区须为 IANA 名称（如 Asia/Shanghai）或 UTC")
    with connect() as conn:
        env = conn.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
        if not env:
            raise HTTPException(404, detail="环境不存在")
        now = time.time()
        conn.execute("UPDATE environments SET timezone=? WHERE id=?", (tz, env_id))
        if not tz:
            # 账实：时区一空，正式资格同事务归零，不留"空时区仍可跨区"的缝
            conn.execute(
                "INSERT INTO env_cross_region(env_id, eligible, official, updated_at, updated_by) "
                "VALUES(?,0,1,?,?) ON CONFLICT(env_id) DO UPDATE SET eligible=0, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by",
                (env_id, now, user["username"]),
            )
        action = "env_timezone_set" if tz else "env_timezone_clear"
        write_audit(conn, user["username"], action, "environment", env_id, tz or "(cleared)")
        write_history(
            conn,
            user["username"],
            "env_timezone",
            f"环境 {env['code']} 时区 {'设为 ' + tz if tz else '清除，跨区资格同步归零'}",
            env_id=env_id,
        )
        conn.commit()
    return {"env_id": env_id, "timezone": tz, "tz_calibrated": bool(tz)}


@router.post("/{env_id}/cross-region")
def set_cross_region(env_id: int, body: CrossRegionIn, authorization: str | None = Header(default=None)):
    """甲路径正式写：缺时区零增拒写；互斥锁争用段独立，败方 409 零正式改。"""
    user = current_user(authorization)
    require_writer(user)
    # 争用段独立：锁行在自有连接里立即落库，与业务写事务隔离
    lock_conn = connect()
    try:
        acquire(lock_conn, DUEL_LOCK_KEY, user["username"])
        lock_conn.commit()
    except Exception:
        lock_conn.close()
        raise
    try:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT e.id, e.code, e.timezone, cr.eligible AS cur
                FROM environments e
                LEFT JOIN env_cross_region cr ON cr.env_id=e.id AND cr.official=1
                WHERE e.id=?
                """,
                (env_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, detail="环境不存在")
            tz = (row["timezone"] or "").strip()
            if body.eligible and not tz:
                # 甲路径：缺时区正式零增拒写，正式区保持零（留拒写审计痕，非正式变更）
                write_audit(
                    conn,
                    user["username"],
                    "env_cross_region_rejected",
                    "env_cross_region",
                    env_id,
                    "缺时区拒写：eligible 0→1（甲路径零增拒写）",
                )
                conn.commit()
                raise HTTPException(409, detail="环境缺时区：可跨区正式标记拒写（甲路径零增拒写）")
            now = time.time()
            conn.execute(
                "INSERT INTO env_cross_region(env_id, eligible, official, updated_at, updated_by) "
                "VALUES(?,?,1,?,?) ON CONFLICT(env_id) DO UPDATE SET eligible=excluded.eligible, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (env_id, 1 if body.eligible else 0, now, user["username"]),
            )
            write_audit(
                conn,
                user["username"],
                "env_cross_region",
                "env_cross_region",
                env_id,
                f"{'grant' if body.eligible else 'revoke'} tz={tz or '(empty)'}",
            )
            write_history(
                conn,
                user["username"],
                "env_cross_region",
                f"环境 {row['code']} {'授予' if body.eligible else '撤销'}可跨区资格",
                env_id=env_id,
            )
            conn.commit()
        return {
            "env_id": env_id,
            "eligible": body.eligible,
            "cross_region": bool(tz and body.eligible),
            "path": "甲路径：同事务正式写，缺时区零增拒写",
        }
    finally:
        release(lock_conn, DUEL_LOCK_KEY, user["username"])
        lock_conn.commit()
        lock_conn.close()
