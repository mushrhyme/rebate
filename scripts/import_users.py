"""
users_import.csv → DB 최초 INSERT 스크립트.

실행:
    cd <프로젝트 루트>
    uv run python -m scripts.import_users

권한 열 매핑:
    관리자 → is_admin = TRUE
    그 외   → is_admin = FALSE
"""
import asyncio
import csv
import sys
from pathlib import Path

import asyncpg
import bcrypt

ROOT = Path(__file__).parents[1]
CSV_PATH = ROOT / "mappings" / "users_import.csv"
ENV_PATH = ROOT / "backend" / ".env"


def _load_db_url() -> str:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    url = env.get("DATABASE_URL", "")
    if not url:
        sys.exit("❌ DATABASE_URL을 .env 파일에서 찾을 수 없습니다")
    return url


def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


async def main() -> None:
    url = _load_db_url()
    conn = await asyncpg.connect(url)
    print(f"✅ DB 연결 완료: {url[:40]}...")

    # 컬럼이 없으면 먼저 추가
    for ddl in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name_ja TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_ko TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_ja TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS category TEXT",
    ]:
        await conn.execute(ddl)

    inserted = skipped = 0
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = row["ID"].strip()
            display_name = row["이름(한글)"].strip()
            display_name_ja = row["名前(日本語)"].strip() or None
            department_ko = row["부서명(한글)"].strip() or None
            department_ja = row["部署(日本語)"].strip() or None
            role = row["권한"].strip() or None
            category = row["분류"].strip() or None
            is_admin = row["권한"].strip() == "관리자"

            existing = await conn.fetchval(
                "SELECT user_id FROM users WHERE username = $1", uid
            )
            if existing:
                print(f"  skip  {uid} (이미 존재)")
                skipped += 1
                continue

            pw_hash = _hash(uid)
            await conn.execute(
                """INSERT INTO users
                       (username, display_name, display_name_ja,
                        department_ko, department_ja, role, category,
                        is_admin, password_hash, force_password_change, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE,TRUE)""",
                uid, display_name, display_name_ja,
                department_ko, department_ja, role, category,
                is_admin, pw_hash,
            )
            print(f"  insert {uid}  {display_name}  ({display_name_ja})")
            inserted += 1

    await conn.close()
    print(f"\n완료: {inserted}명 추가, {skipped}명 건너뜀")


if __name__ == "__main__":
    asyncio.run(main())
