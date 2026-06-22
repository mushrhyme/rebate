"""인증 + 사용자 관리 라우트 — JWT + S3 users.json 기반."""
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...core.auth import get_current_user, require_admin
from ...core.config import get_settings
from ...db.queries import _find_user, _read_users, _write_users

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _issue_token(user: dict) -> str:
    settings = get_settings()
    payload = {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user.get("display_name"),
        "display_name_ja": user.get("display_name_ja"),
        "department_ko": user.get("department_ko"),
        "department_ja": user.get("department_ja"),
        "role": user.get("role"),
        "category": user.get("category"),
        "is_admin": user.get("is_admin", False) or user.get("username") == "admin",
        "is_active": user.get("is_active", True),
        "force_password_change": user.get("force_password_change", False),
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


# ── 인증 ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
async def login(body: LoginRequest):
    users = _read_users()
    user = _find_user(users, username=body.username)
    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자 없음")

    if not bcrypt.checkpw(body.password.encode()[:72], user["password_hash"].encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="비밀번호 불일치")

    # login_count + last_login_at 업데이트
    user["login_count"] = user.get("login_count", 0) + 1
    user["last_login_at"] = datetime.now(timezone.utc).isoformat()
    _write_users(users)

    token = _issue_token(user)
    return {
        "session_id": token,  # 프론트가 X-Session-Id 헤더로 전송하는 값 — 필드명 유지
        "user": {
            "user_id": user["user_id"],
            "username": user["username"],
            "display_name": user.get("display_name"),
            "display_name_ja": user.get("display_name_ja"),
            "is_admin": user.get("is_admin", False) or user.get("username") == "admin",
            "force_password_change": user.get("force_password_change", False),
        },
    }


@router.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user


@router.get("/validate-session")
async def validate_session(user: dict = Depends(get_current_user)):
    return {"valid": True, "user": user}


@router.post("/refresh")
async def refresh(user: dict = Depends(get_current_user)):
    """슬라이딩 갱신 — 아직 유효한 토큰으로 만료시각이 새로워진 토큰을 재발급한다.

    활동 중인 사용자가 고정 만료(jwt_expire_hours)로 작업 도중 끊기지 않게 한다.
    프론트가 주기적으로(그리고 탭 포커스 시) 호출 → 앱을 열고 쓰는 동안 세션이 유지된다.
    그 사이 비활성화·삭제된 사용자는 갱신을 거부(즉시 세션 만료 처리)."""
    users = _read_users()
    target = _find_user(users, user_id=user["user_id"])
    if not target or not target.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="세션 만료")
    return {"session_id": _issue_token(target)}


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    users = _read_users()
    target = _find_user(users, user_id=user["user_id"])
    if not target:
        raise HTTPException(status_code=404, detail="사용자 없음")

    if not bcrypt.checkpw(body.current_password.encode()[:72], target["password_hash"].encode()):
        raise HTTPException(status_code=400, detail="현재 비밀번호 불일치")

    if body.new_password == target["username"]:
        raise HTTPException(status_code=400, detail="ログインIDと同一のパスワードは使用できません")

    target["password_hash"] = bcrypt.hashpw(body.new_password.encode()[:72], bcrypt.gensalt()).decode()
    target["force_password_change"] = False
    _write_users(users)
    return {"ok": True}


# ── 사용자 관리 (관리자 전용) ─────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    display_name_ja: Optional[str] = None
    department_ko: Optional[str] = None
    department_ja: Optional[str] = None
    role: Optional[str] = None
    category: Optional[str] = None
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    display_name_ja: Optional[str] = None
    department_ko: Optional[str] = None
    department_ja: Optional[str] = None
    role: Optional[str] = None
    category: Optional[str] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    reset_password: bool = False


@router.get("/users")
async def list_users(admin: dict = Depends(require_admin)):
    users = _read_users()
    return [
        {k: v for k, v in u.items() if k != "password_hash"}
        for u in users
    ]


@router.post("/users", status_code=201)
async def create_user(body: CreateUserRequest, admin: dict = Depends(require_admin)):
    users = _read_users()
    if _find_user(users, username=body.username):
        raise HTTPException(status_code=400, detail="이미 존재하는 ID입니다")

    new_id = max((u["user_id"] for u in users), default=0) + 1
    pw_hash = bcrypt.hashpw(body.username.encode()[:72], bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()
    new_user = {
        "user_id": new_id,
        "username": body.username,
        "display_name": body.display_name,
        "display_name_ja": body.display_name_ja,
        "department_ko": body.department_ko,
        "department_ja": body.department_ja,
        "role": body.role,
        "category": body.category,
        "is_admin": body.is_admin,
        "is_active": True,
        "force_password_change": True,
        "password_hash": pw_hash,
        "login_count": 0,
        "last_login_at": None,
        "created_at": now,
    }
    users.append(new_user)
    _write_users(users)
    return {"user_id": new_id, "ok": True}


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UpdateUserRequest, admin: dict = Depends(require_admin)):
    users = _read_users()
    target = _find_user(users, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="사용자 없음")

    for field in ("display_name", "display_name_ja", "department_ko", "department_ja",
                  "role", "category", "is_active", "is_admin"):
        v = getattr(body, field)
        if v is not None:
            target[field] = v

    if body.reset_password:
        target["password_hash"] = bcrypt.hashpw(target["username"].encode()[:72], bcrypt.gensalt()).decode()
        target["force_password_change"] = True

    _write_users(users)
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    users = _read_users()
    new_users = [u for u in users if u["user_id"] != user_id]
    if len(new_users) == len(users):
        raise HTTPException(status_code=404, detail="사용자 없음")
    _write_users(new_users)
    return {"ok": True}
