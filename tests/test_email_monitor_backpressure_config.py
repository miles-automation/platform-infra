from pathlib import Path


def test_email_monitor_exposes_bounded_notification_controls() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "docker-compose.yml").read_text()
    dotenv = (root / ".env.example").read_text()
    expected = {
        "EMAIL_MONITOR_MAX_NOTIFICATIONS_PER_ITERATION": "20",
        "MATRIX_SEND_MAX_ATTEMPTS": "3",
        "MATRIX_RETRY_BASE_SECONDS": "1",
        "MATRIX_RETRY_MAX_SECONDS": "30",
    }

    for name, default in expected.items():
        assert f"{name}: ${{{name}:-{default}}}" in compose
        assert f"{name}={default}" in dotenv
