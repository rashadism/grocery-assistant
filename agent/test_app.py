import unittest

from app import _evidence_actually_recommended


class EvidenceActuallyRecommendedTest(unittest.TestCase):
    def setUp(self):
        self.evidence = [
            {"url": "https://x/a", "name": "A"},
            {"url": "https://x/b", "name": "B"},
            {"url": "https://x/c", "name": "C"},
            {"url": "https://x/d", "name": "D"},
        ]

    def test_keeps_only_products_linked_in_the_reply_in_mention_order(self):
        reply = "Go with [C](https://x/c) or [A](https://x/a)."
        kept = _evidence_actually_recommended(reply, self.evidence)
        self.assertEqual([e["name"] for e in kept], ["C", "A"])

    def test_no_links_in_reply_means_no_evidence_shown(self):
        self.assertEqual(_evidence_actually_recommended("No picks today.", self.evidence), [])

    def test_a_link_to_an_unknown_url_is_ignored(self):
        reply = "See [mystery](https://not-in-evidence)."
        self.assertEqual(_evidence_actually_recommended(reply, self.evidence), [])


if __name__ == "__main__":
    unittest.main()
