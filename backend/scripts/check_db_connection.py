import os

import psycopg
from dotenv import load_dotenv


load_dotenv()

database_url = os.getenv("DATABASE_URL", "")

database_url = database_url.replace(
    "postgresql+psycopg://",
    "postgresql://",
)

if not database_url:
    raise RuntimeError("DATABASE_URL is not configured")


with psycopg.connect(database_url) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version();")
        version = cursor.fetchone()

        print("Connected to PostgreSQL!")
        print(version[0])