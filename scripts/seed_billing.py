"""Load the demo world into a running Billing service: uv run python scripts/seed_billing.py [--url http://127.0.0.1:8010]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from billingops.billing_client import BillingClient  # noqa: E402
from billingops.testing import duplicate_world  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--admin-token", default="demo-admin")
    parser.add_argument("--prior-refund-cents", type=int, default=0)
    args = parser.parse_args()
    client = BillingClient(args.url, admin_token=args.admin_token)
    print(client.load_world(duplicate_world(prior_refund_cents=args.prior_refund_cents)))


if __name__ == "__main__":
    main()
