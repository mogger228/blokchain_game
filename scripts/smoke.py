"""Безопасный smoke-сценарий из README: работает только во временной БД."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import service  # noqa: E402
import storage  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        storage.configure_db_path(Path(directory) / "smoke.db")
        service.initialize()
        service.register_player(101, "smoke_alice")
        service.register_player(202, "smoke_bob")

        transfer = service.create_player_transaction(
            101, "smoke_bob", 25, "smoke-transfer"
        )
        assert service.get_player_balance(101).balance == 75.0
        assert service.get_player_balance(202).balance == 125.0

        grant = service.create_admin_grant(
            999, "smoke_bob", 50, "smoke-grant", configured_admin_id=999
        )
        assert service.get_player_balance(101).balance == 75.0
        assert service.get_player_balance(202).balance == 175.0
        assert service.get_transaction(transfer.index).hash == transfer.hash
        assert service.get_transaction(grant.index).hash == grant.hash
        assert service.check_chain() == (True, None)

        page = service.list_players_with_balances()
        assert page.total == 2
        print(
            "SMOKE PASS: registrations=2, balances=75/175, "
            f"transfer_id={transfer.index}, grant_id={grant.index}, chain=valid"
        )
    storage.configure_db_path(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

