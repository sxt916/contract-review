import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_standalone_compose() -> dict:
    compose_path = PROJECT_ROOT / "deploy" / "docker-compose.standalone.yml"
    return json.loads(compose_path.read_text(encoding="utf-8"))


def test_standalone_compose_exposes_web_and_shares_persistent_data():
    compose = load_standalone_compose()
    services = compose["services"]

    assert services["contract-review-web"]["ports"] == [
        "${CONTRACT_REVIEW_PORT:-8000}:8000"
    ]
    assert services["contract-review-web"]["volumes"] == [
        "contract-review-data:/data"
    ]
    assert services["contract-review-worker"]["volumes"] == [
        "contract-review-data:/data"
    ]
    assert compose["volumes"] == {"contract-review-data": {}}
    assert "networks" not in compose


def test_standalone_compose_reads_untracked_environment_file():
    compose = load_standalone_compose()

    for service in compose["services"].values():
        assert service["image"] == "${CONTRACT_REVIEW_IMAGE:-contract-review:latest}"
        assert service["env_file"] == [".env"]
        assert service["environment"]["DATA_DIR"] == "/data"
