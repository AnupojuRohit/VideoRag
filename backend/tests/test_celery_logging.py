import json
import logging

from celery.signals import setup_logging, worker_process_init

from app.celery_app import celery_app


def test_celery_worker_and_forked_process_use_json_logging(
    capsys,
) -> None:
    root_logger = logging.getLogger()
    existing_handlers = root_logger.handlers[:]
    existing_level = root_logger.level
    root_logger.handlers.clear()
    try:
        setup_logging.send(sender=celery_app, loglevel=logging.INFO)
        worker_process_init.send(sender=celery_app)

        logging.getLogger("app.providers.gemini_vision").warning(
            "Provider request failed",
            extra={
                "operation": "vision",
                "http_status": 503,
                "google_error_code": 503,
                "google_error_status": "UNAVAILABLE",
                "google_error_message": "Temporary failure",
                "request_id": "test-request",
            },
        )

        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["level"] == "WARNING"
        assert payload["message"] == "Provider request failed"
        assert payload["operation"] == "vision"
        assert payload["http_status"] == "503"
        assert payload["google_error_code"] == "503"
        assert payload["google_error_status"] == "UNAVAILABLE"
        assert payload["google_error_message"] == "Temporary failure"
        assert payload["request_id"] == "test-request"
    finally:
        root_logger.handlers.clear()
        root_logger.handlers.extend(existing_handlers)
        root_logger.setLevel(existing_level)

    assert celery_app.conf.worker_hijack_root_logger is False
