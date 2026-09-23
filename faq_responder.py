"""Safe, deterministic FAQ email responder core.

The module intentionally separates decision-making from email delivery.  A Gmail
adapter can send only decisions whose action is ``reply``; uncertain messages are
always left for a person.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Protocol


WORD_RE = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(WORD_RE.findall(text.lower()))


def score(message: str, question: str, keywords: Iterable[str]) -> float:
    message_norm = normalize(message)
    question_norm = normalize(question)
    if not message_norm or not question_norm:
        return 0.0

    similarity = SequenceMatcher(None, message_norm, question_norm).ratio()
    message_words = set(message_norm.split())
    question_words = set(question_norm.split())
    overlap = len(message_words & question_words) / max(1, len(question_words))

    normalized_keywords = [normalize(value) for value in keywords]
    normalized_keywords = [value for value in normalized_keywords if value]
    keyword_hits = sum(value in message_norm for value in normalized_keywords)
    keyword_score = keyword_hits / max(1, len(normalized_keywords))

    return round((0.45 * similarity) + (0.35 * overlap) + (0.20 * keyword_score), 4)


@dataclass(frozen=True)
class Decision:
    message_id: str
    action: str
    confidence: float
    faq_id: str | None = None
    reply: str | None = None
    reason: str | None = None


class FAQResponder:
    def __init__(self, faq_items: list[dict], threshold: float = 0.72):
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        self.faq_items = faq_items
        self.threshold = threshold

    def decide(self, message_id: str, subject: str, body: str) -> Decision:
        text = f"{subject}\n{body}".strip()
        ranked = sorted(
            (
                score(text, item["question"], item.get("keywords", [])),
                item,
            )
            for item in self.faq_items
        )
        if not ranked:
            return Decision(message_id, "manual_review", 0.0, reason="no_faq_items")

        best_score, best_item = ranked[-1]
        runner_up = ranked[-2][0] if len(ranked) > 1 else 0.0
        if best_score < self.threshold:
            return Decision(message_id, "manual_review", best_score, reason="low_confidence")
        if best_score - runner_up < 0.08:
            return Decision(message_id, "manual_review", best_score, reason="ambiguous_match")

        return Decision(
            message_id=message_id,
            action="reply",
            confidence=best_score,
            faq_id=best_item["id"],
            reply=best_item["answer"],
        )


class EmailGateway(Protocol):
    """Minimum interface required from Gmail or another email provider."""

    def list_unhandled(self) -> list[dict]: ...

    def create_draft(self, message_id: str, body: str) -> str: ...

    def mark_for_review(self, message_id: str) -> None: ...


class ProcessedStore:
    """Durable idempotency store that prevents replies across repeated runs."""

    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS processed_messages "
            "(message_id TEXT PRIMARY KEY, action TEXT NOT NULL, processed_at TEXT NOT NULL)"
        )
        self.connection.commit()

    def contains(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def add(self, message_id: str, action: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO processed_messages VALUES (?, ?, ?)",
            (message_id, action, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def write(self, decision: Decision, delivery_id: str | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": decision.message_id,
            "action": decision.action,
            "confidence": decision.confidence,
            "faq_id": decision.faq_id,
            "reason": decision.reason,
            "delivery_id": delivery_id,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def process_gateway(
    responder: FAQResponder,
    gateway: EmailGateway,
    store: ProcessedStore,
    audit: AuditLog,
) -> list[Decision]:
    """Create drafts for safe matches and route everything else to a person.

    This function never sends email.  A separate, explicitly approved release
    step is required to send Gmail drafts.
    """
    decisions: list[Decision] = []
    for message in gateway.list_unhandled():
        message_id = str(message["id"])
        if store.contains(message_id):
            decisions.append(Decision(message_id, "skip", 1.0, reason="already_processed"))
            continue

        decision = responder.decide(
            message_id, message.get("subject", ""), message.get("body", "")
        )
        delivery_id = None
        if decision.action == "reply":
            delivery_id = gateway.create_draft(message_id, decision.reply or "")
        else:
            gateway.mark_for_review(message_id)
        store.add(message_id, decision.action)
        audit.write(decision, delivery_id)
        decisions.append(decision)
    return decisions


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_batch(faq_path: Path, messages_path: Path, threshold: float) -> list[Decision]:
    responder = FAQResponder(load_json(faq_path), threshold)
    seen: set[str] = set()
    decisions: list[Decision] = []
    for message in load_json(messages_path):
        message_id = str(message["id"])
        if message_id in seen:
            decisions.append(
                Decision(message_id, "skip", 1.0, reason="duplicate_message_id")
            )
            continue
        seen.add(message_id)
        decisions.append(
            responder.decide(message_id, message.get("subject", ""), message.get("body", ""))
        )
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely classify FAQ email messages")
    parser.add_argument("--faq", type=Path, default=Path("sample_faq.json"))
    parser.add_argument("--messages", type=Path, default=Path("sample_messages.json"))
    parser.add_argument("--threshold", type=float, default=0.72)
    args = parser.parse_args()
    decisions = run_batch(args.faq, args.messages, args.threshold)
    print(json.dumps([asdict(item) for item in decisions], indent=2))


if __name__ == "__main__":
    main()
