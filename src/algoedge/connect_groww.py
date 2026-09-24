from algoedge.config import get_settings
from algoedge.groww_broker import GrowwBroker


def main() -> None:
    settings = get_settings()
    broker = GrowwBroker.from_settings(settings)
    broker.verify_connection()
    print("Groww connection succeeded.")


if __name__ == "__main__":
    main()
