"""
Issues a signed merchant API token for production auth mode.

Usage (from the repository root):
    $env:PYTHONPATH="backend"
    python backend/scripts/issue_merchant_token.py <merchant_uuid>

Requires AUTH_TOKEN_SECRET in backend/.env (or the environment).
Send the token as:  Authorization: Bearer <token>
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.security import issue_merchant_token  # noqa: E402


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    print(issue_merchant_token(sys.argv[1]))
