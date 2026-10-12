"""``python -m billing_service --db billing.sqlite3 --port 8010``"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated Billing service")
    parser.add_argument("--db", default="data/billing.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
