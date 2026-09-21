"""哈希链账本防篡改与封存校验测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crop_spray import Ledger
from crop_spray.ledger import digest


class LedgerTest(unittest.TestCase):
    def test_chain_links_and_head(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "l.jsonl"
            led = Ledger(path)
            e1 = led.append("T", "a", {"x": 1})
            e2 = led.append("T", "a", {"x": 2})
            self.assertEqual(e2.prev_hash, e1.hash)
            self.assertEqual(led.head, e2.hash)
            # 重新打开自动校验
            led2 = Ledger(path)
            self.assertEqual(led2.head, e2.hash)

    def test_tamper_payload_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "l.jsonl"
            led = Ledger(path)
            led.append("T", "a", {"x": 1})
            led.append("T", "a", {"x": 2})
            lines = path.read_text("utf-8").splitlines()
            obj = json.loads(lines[0])
            obj["payload"]["x"] = 999
            lines[0] = json.dumps(obj, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", "utf-8")
            with self.assertRaisesRegex(Exception, "篡改"):
                Ledger(path)

    def test_replay_after_delete_event_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "l.jsonl"
            led = Ledger(path)
            led.append("T", "a", {"x": 1})
            led.append("T", "a", {"x": 2})
            lines = path.read_text("utf-8").splitlines()
            path.write_text(lines[1] + "\n", "utf-8")
            with self.assertRaisesRegex(Exception, "序号断裂|哈希链断裂"):
                Ledger(path)

    def test_canonical_digest_stable(self):
        a = digest({"b": 1, "a": "中文"})
        b = digest({"a": "中文", "b": 1})
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
