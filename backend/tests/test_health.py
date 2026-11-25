from fastapi.testclient import TestClient

from app import main


def test_liveness_is_independent_of_dependencies() -> None:
    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_success_when_dependencies_are_available(
    monkeypatch,
) -> None:
    async def available() -> None:
        return None

    monkeypatch.setattr(main, "check_database", available)
    monkeypatch.setattr(main, "check_redis", available)

    with TestClient(main.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "redis": True},
    }


def test_readiness_returns_service_unavailable_when_dependency_fails(
    monkeypatch,
) -> None:
    async def unavailable() -> None:
        raise OSError("connection refused")

    async def available() -> None:
        return None

    monkeypatch.setattr(main, "check_database", unavailable)
    monkeypatch.setattr(main, "check_redis", available)

    with TestClient(main.app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": False, "redis": True},
    }
