"""JWT 기반 인증 — PostgreSQL session 제거."""
import jwt
from fastapi import Depends, Header, HTTPException, Query, status

from .config import get_settings


def _decode_token(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="세션 없음")
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="세션 만료")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰 오류")

    if not payload.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="비활성 사용자")
    return payload


async def get_current_user(x_session_id: str | None = Header(default=None)) -> dict:
    return _decode_token(x_session_id or "")


async def get_current_user_sse(
    x_session_id: str | None = Header(default=None),
    sid: str | None = Query(default=None),
) -> dict:
    """SSE 전용 — EventSource는 커스텀 헤더 불가하므로 ?sid= 쿼리 파라미터도 허용."""
    return _decode_token(x_session_id or sid or "")


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin") and user.get("username") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자 권한 필요")
    return user
