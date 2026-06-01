from fastapi import Depends, Header, HTTPException, Query, status
from .database import get_pool


async def _resolve_session(session_id: str | None) -> dict:
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="세션 없음")
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.user_id, u.username, u.display_name, u.display_name_ja,
               u.is_admin, u.force_password_change,
               u.department_ko, u.department_ja, u.role, u.category
        FROM user_sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.session_id = $1
          AND s.expires_at > NOW()
          AND u.is_active = TRUE
        """,
        session_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="세션 만료")
    return dict(row)


async def get_current_user(x_session_id: str | None = Header(default=None)) -> dict:
    return await _resolve_session(x_session_id)


async def get_current_user_sse(
    x_session_id: str | None = Header(default=None),
    sid: str | None = Query(default=None),
) -> dict:
    """SSE 전용 — EventSource는 커스텀 헤더 불가하므로 ?sid= 쿼리 파라미터도 허용."""
    return await _resolve_session(x_session_id or sid)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin") and user.get("username") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자 권한 필요")
    return user
