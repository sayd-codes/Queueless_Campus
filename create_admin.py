"""Create an admin account (or turn an existing account into an admin).

Run:  python create_admin.py
"""
import getpass
import sys

from app import auth, db


def main() -> int:
    db.init_db()
    print("Create an admin account for QueueLess Campus\n")
    email = input("Admin email: ").strip()
    existing = next((u for u in auth.list_users() if u["email"] == auth.normalize_email(email)), None)
    if existing:
        if existing["role"] == "admin":
            print("That account is already an admin.")
            return 0
        auth.set_role(existing["id"], "admin")
        print(f"{existing['email']} is now an admin.")
        return 0
    name = input("Full name: ").strip()
    password = getpass.getpass("Password (min 8 characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1
    try:
        user = auth.create_user(email, password, name, role="admin")
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    print(f"\nAdmin created: {user['email']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
