"""修改管理员密码的独立脚本，运行后交互式输入用户名和新密码"""
import sys
import os
import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.core.database import init_db, get_connection
from api.core.security import get_password_hash, verify_password


def reset_password():
    username = input("请输入用户名: ").strip()
    if not username:
        print("用户名不能为空")
        return

    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            print(f"用户 '{username}' 不存在")
            return

    new_password = getpass.getpass("请输入新密码: ")
    if len(new_password) < 4:
        print("密码至少4个字符")
        return

    confirm = getpass.getpass("确认新密码: ")
    if new_password != confirm:
        print("两次密码不一致")
        return

    hashed = get_password_hash(new_password)
    with get_connection() as conn:
        conn.execute("UPDATE users SET hashed_password = ? WHERE username = ?", (hashed, username))

    print(f"用户 '{username}' 密码已更新")


if __name__ == "__main__":
    init_db()
    reset_password()
