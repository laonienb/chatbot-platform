"""把指定邮箱的用户提升为管理员。用法：python -m scripts.make_admin <email>"""

import asyncio
import sys

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import User


async def main(email: str) -> None:
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            print(f"用户不存在: {email}")
            sys.exit(1)
        await db.execute(update(User).where(User.id == user.id).values(role="admin"))
        await db.commit()
        print(f"已提升为管理员: {email}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python -m scripts.make_admin <email>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
