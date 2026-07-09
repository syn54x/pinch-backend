import subprocess
import sys


def test_cli_import() -> None:
    import pinch_cli

    assert pinch_cli.__version__


def test_cli_never_imports_backend() -> None:
    """ADR 0001: pinch-cli is a pure API client — importing it must not pull
    in pinch_backend. Run in a subprocess for a clean module table."""
    code = (
        "import sys; import pinch_cli.app; "
        "assert not [m for m in sys.modules if m.startswith('pinch_backend')]"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
