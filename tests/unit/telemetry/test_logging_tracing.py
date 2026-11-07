import json

from hapm_telemetry import configure_logging, configure_tracing, get_logger


def test_logs_are_json_with_service_and_context(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("test-svc", "INFO")
    get_logger(run_id="r1").info("hello", step="plan")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["service"] == "test-svc"
    assert line["run_id"] == "r1"
    assert line["event"] == "hello"
    assert line["level"] == "info"


def test_debug_is_filtered_at_info(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("test-svc", "INFO")
    get_logger().debug("hidden")
    assert "hidden" not in capsys.readouterr().out


def test_tracing_without_endpoint_has_no_exporter() -> None:
    provider = configure_tracing("svc")
    assert provider.resource.attributes["service.name"] == "svc"


def test_tracing_with_endpoint_adds_exporter() -> None:
    provider = configure_tracing("svc", "http://collector:4318/")
    processors = provider._active_span_processor._span_processors
    assert len(processors) == 1
    provider.shutdown()
