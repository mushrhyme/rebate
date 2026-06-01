"""인증 + 사용자 관리 라우트."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...core.auth import get_current_user, require_admin
from ...core.database import get_pool

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_TTL_HOURS = 12


# ── 인증 ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
async def login(body: LoginRequest):
    pool = get_pool()
    user = await pool.fetchrow(
        """SELECT user_id, username, display_name, display_name_ja,
                  password_hash, is_admin, is_active, force_password_change
           FROM users WHERE username = $1 AND is_active = TRUE""",
        body.username,
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자 없음")

    pw_bytes = body.password.encode()[:72]
    if not bcrypt.checkpw(pw_bytes, user["password_hash"].encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="비밀번호 불일치")

    session_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    await pool.execute(
        "INSERT INTO user_sessions (session_id, user_id, expires_at) VALUES ($1, $2, $3)",
        session_id, user["user_id"], expires_at,
    )
    await pool.execute(
        "UPDATE users SET login_count = login_count + 1, last_login_at = NOW() WHERE user_id = $1",
        user["user_id"],
    )

    return {
        "session_id": session_id,
        "user": {
            "user_id": user["user_id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "display_name_ja": user["display_name_ja"],
            "is_admin": user["is_admin"] or user["username"] == "admin",
            "force_password_change": user["force_password_change"],
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


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    pool = get_pool()
    row = await pool.fetchrow("SELECT password_hash FROM users WHERE user_id = $1", user["user_id"])
    if not bcrypt.checkpw(body.current_password.encode()[:72], row["password_hash"].encode()):
        raise HTTPException(status_code=400, detail="현재 비밀번호 불일치")

    if body.new_password == user["username"]:
        raise HTTPException(status_code=400, detail="ログインIDと同一のパスワードは使用できません")

    new_hash = bcrypt.hashpw(body.new_password.encode()[:72], bcrypt.gensalt()).decode()
    await pool.execute(
        "UPDATE users SET password_hash = $1, force_password_change = FALSE WHERE user_id = $2",
        new_hash, user["user_id"],
    )
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
    reset_password: bool = False  # True면 비밀번호를 username으로 초기화


@router.get("/users")
async def list_users(admin: dict = Depends(require_admin)):
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT user_id, username, display_name, display_name_ja,
                  department_ko, department_ja, role, category,
                  is_admin, is_active, force_password_change,
                  login_count, last_login_at, created_at
           FROM users
           ORDER BY created_at"""
    )
    return [dict(r) for r in rows]


@router.post("/users", status_code=201)
async def create_user(body: CreateUserRequest, admin: dict = Depends(require_admin)):
    pool = get_pool()
    existing = await pool.fetchrow("SELECT user_id FROM users WHERE username = $1", body.username)
    if existing:
        raise HTTPException(status_code=400, detail="이미 존재하는 ID입니다")

    pw_hash = bcrypt.hashpw(body.username.encode()[:72], bcrypt.gensalt()).decode()
    row = await pool.fetchrow(
        """INSERT INTO users
               (username, display_name, display_name_ja, department_ko, department_ja,
                role, category, is_admin, password_hash, force_password_change)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
           RETURNING user_id""",
        body.username, body.display_name, body.display_name_ja,
        body.department_ko, body.department_ja,
        body.role, body.category, body.is_admin, pw_hash,
    )
    return {"user_id": row["user_id"], "ok": True}


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UpdateUserRequest, admin: dict = Depends(require_admin)):
    pool = get_pool()
    target = await pool.fetchrow("SELECT username FROM users WHERE user_id = $1", user_id)
    if not target:
        raise HTTPException(status_code=404, detail="사용자 없음")

    sets, vals = [], []
    for field in ("display_name", "display_name_ja", "department_ko", "department_ja",
                  "role", "category", "is_active", "is_admin"):
        v = getattr(body, field)
        if v is not None:
            sets.append(f"{field} = ${len(vals)+1}")
            vals.append(v)

    if body.reset_password:
        new_hash = bcrypt.hashpw(target["username"].encode()[:72], bcrypt.gensalt()).decode()
        sets.append(f"password_hash = ${len(vals)+1}")
        vals.append(new_hash)
        sets.append(f"force_password_change = ${len(vals)+1}")
        vals.append(True)

    if not sets:
        return {"ok": True}

    vals.append(user_id)
    await pool.execute(
        f"UPDATE users SET {', '.join(sets)} WHERE user_id = ${len(vals)}",
        *vals,
    )
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    pool = get_pool()
    result = await pool.execute("DELETE FROM users WHERE user_id = $1", user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="사용자 없음")
    return {"ok": True}
