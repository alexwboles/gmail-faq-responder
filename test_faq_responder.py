import unittest
import tempfile
from pathlib import Path

from faq_responder import (
    AuditLog,
    FAQResponder,
    ProcessedStore,
    normalize,
    process_gateway,
    score,
)


FAQ = [
    {
        "id": "schedule",
        "question": "What is the class schedule?",
        "keywords": ["class schedule", "class times"],
        "answer": "See our schedule.",
    },
    {
        "id": "price",
        "question": "How much does membership cost?",
        "keywords": ["membership cost", "monthly fee"],
        "answer": "Plans start at $49.",
    },
]


class FAQResponderTests(unittest.TestCase):
    def setUp(self):
        self.responder = FAQResponder(FAQ, threshold=0.72)

    def test_normalize(self):
        self.assertEqual(normalize("  Class TIMES?! "), "class times")

    def test_known_question_is_replied_to(self):
        decision = self.responder.decide("1", "Class schedule", "What is the class schedule?")
        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.faq_id, "schedule")
        self.assertGreaterEqual(decision.confidence, 0.72)

    def test_unknown_question_is_left_for_manual_review(self):
        decision = self.responder.decide("2", "Injury", "What should I do about knee pain?")
        self.assertEqual(decision.action, "manual_review")
        self.assertEqual(decision.reason, "low_confidence")

    def test_empty_faq_is_safe(self):
        decision = FAQResponder([]).decide("3", "Hello", "Anyone there?")
        self.assertEqual(decision.action, "manual_review")
        self.assertEqual(decision.reason, "no_faq_items")

    def test_keyword_match_contributes_to_score(self):
        with_keyword = score("Please share class times", "What is the class schedule?", ["class times"])
        without_keyword = score("Please share class times", "What is the class schedule?", [])
        self.assertGreater(with_keyword, without_keyword)

    def test_gateway_creates_drafts_and_routes_unknown_messages(self):
        class FakeGateway:
            def __init__(self):
                self.messages = [
                    {"id": "known", "subject": "Class schedule", "body": "What is the class schedule?"},
                    {"id": "unknown", "subject": "Health", "body": "Can you diagnose my knee pain?"},
                ]
                self.drafts = []
                self.review = []

            def list_unhandled(self):
                return self.messages

            def create_draft(self, message_id, body):
                self.drafts.append((message_id, body))
                return f"draft-{message_id}"

            def mark_for_review(self, message_id):
                self.review.append(message_id)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProcessedStore(root / "state.db")
            gateway = FakeGateway()
            decisions = process_gateway(
                self.responder, gateway, store, AuditLog(root / "audit.jsonl")
            )
            self.assertEqual([item.action for item in decisions], ["reply", "manual_review"])
            self.assertEqual(gateway.drafts[0][0], "known")
            self.assertEqual(gateway.review, ["unknown"])
            self.assertEqual(len((root / "audit.jsonl").read_text().splitlines()), 2)
            store.close()

    def test_durable_store_prevents_duplicate_draft_across_runs(self):
        class FakeGateway:
            def __init__(self):
                self.drafts = []

            def list_unhandled(self):
                return [{"id": "same-id", "subject": "Class schedule", "body": "What is the class schedule?"}]

            def create_draft(self, message_id, body):
                self.drafts.append(message_id)
                return "draft-1"

            def mark_for_review(self, message_id):
                raise AssertionError("known question should not require review")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = FakeGateway()
            store = ProcessedStore(root / "state.db")
            audit = AuditLog(root / "audit.jsonl")
            first = process_gateway(self.responder, gateway, store, audit)
            second = process_gateway(self.responder, gateway, store, audit)
            self.assertEqual(first[0].action, "reply")
            self.assertEqual(second[0].reason, "already_processed")
            self.assertEqual(gateway.drafts, ["same-id"])
            store.close()


if __name__ == "__main__":
    unittest.main()
