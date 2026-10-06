from pathlib import Path

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def main() -> None:
    from .server import main as run

    run()
