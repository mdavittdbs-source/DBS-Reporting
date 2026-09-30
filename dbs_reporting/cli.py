"""Terminal chat: `python -m dbs_reporting.cli`."""

from .agent import create_agent
from .config import ConnectWiseSettings
from .connectwise import ConnectWiseClient


def main() -> None:
    agent = create_agent(ConnectWiseClient(ConnectWiseSettings.from_env()))
    history: list = []
    print(f"Using {agent.description}.")
    print("DBS reporting assistant. Ask about a client, e.g. \"most common issues at Joe's Pizza "
          "in the last 30 days\". Ctrl+C to quit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        answer, history = agent.respond(history, question)
        print("\n" + answer)


if __name__ == "__main__":
    main()
