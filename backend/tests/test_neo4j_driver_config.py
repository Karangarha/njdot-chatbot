"""backend/tests/test_neo4j_driver_config.py

The Neo4j singleton must (a) keep liveness_check_timeout -- it fixed a
production SessionExpired after idle -- and (b) disable the UNRECOGNIZED
notification class that floods the log on every review (sandbox noise).

Runnable two ways:
    python backend/tests/test_neo4j_driver_config.py
    python -m pytest backend/tests/test_neo4j_driver_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import neo4j_client  # noqa: E402


def _driver_config():
    neo4j_client.Neo4jClient._instance = None
    with patch.object(neo4j_client, "Neo4jGraph") as graph_cls, \
         patch.object(neo4j_client.config, "NEO4J_URI", "bolt://x:7687"), \
         patch.object(neo4j_client.config, "NEO4J_PASSWORD", "pw"):
        neo4j_client.Neo4jClient.get_graph()
    neo4j_client.Neo4jClient._instance = None
    return graph_cls.call_args.kwargs["driver_config"]


def test_driver_config_keeps_liveness_check():
    assert _driver_config()["liveness_check_timeout"] == 60


def test_driver_config_disables_unrecognized_notifications():
    assert _driver_config()["notifications_disabled_classifications"] == ["UNRECOGNIZED"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed")
