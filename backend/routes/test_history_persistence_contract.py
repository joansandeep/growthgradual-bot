"""Source-level regression checks for the frontend conversation persistence contract."""
from pathlib import Path


def main() -> None:
    p = Path(__file__).resolve().parents[2] / "frontend/src/components/GrowthGradualChat.tsx"
    src = p.resolve().read_text(encoding="utf-8")
    required = [
        "messagesRef",
        "activeIdRef",
        "conversationsRef",
        "beforeunload",
        "saveConversations(next)",
        "saveConversations(base)",
        "setMessages(cleanedMessages)",
    ]
    missing = [x for x in required if x not in src]
    assert not missing, f"missing history persistence contract markers: {missing}"
    print("PASS: frontend history persistence contract present")


if __name__ == "__main__":
    main()
