"""Terminal chat: `python -m dbs_reporting.cli`."""

from .agent import create_agent
from .config import ConnectWiseSettings
from .connectwise import ConnectWiseClient


def main() -> None:
    agent = create_agent(ConnectWiseClient(ConnectWiseSettings.from_env()))
    history: list = []
    print(f"Using {agent.description}.")
    print("DBS Automated Virtual Information Desk. Ask about a client, e.g. \"most common issues at Jimmy's Grille "
          "in the last 30 days\". Ctrl+C to quit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        print()
        streamed = False
        for event in agent.respond_stream(history, question):
            if event["type"] == "status":
                print(f"  … {event['text']}", flush=True)
            elif event["type"] == "reset" and streamed:
                print()  # a new step started; keep earlier partial text on its own line
                streamed = False
            elif event["type"] == "text":
                if not streamed:
                    print()
                print(event["text"], end="", flush=True)
                streamed = True
            elif event["type"] == "done":
                history = event["history"]
                print()


if __name__ == "__main__":
    main()
