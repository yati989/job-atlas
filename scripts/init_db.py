"""
One-time DB setup: create all tables defined in app.models.orm.

Run with:
    python -m scripts.init_db
"""
from app.db.session import engine
from app.models.orm import Base


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("Tables created: companies, jobs")


if __name__ == "__main__":
    main()
