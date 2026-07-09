import os


def pytest_configure() -> None:
    os.environ.setdefault("LOGFIRE_SEND_TO_LOGFIRE", "false")
