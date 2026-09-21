"""只追加哈希链事件账本。

每条事件：seq / at / type / actor / payload / prev_hash / hash。
篡改、删除、重排任何一条事件都会使链校验失败。
账本是唯一事实来源；业务状态由重放事件得到。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def canonical(obj) -> bytes:
    """确定性序列化：键排序、无多余空白、不转义中文。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Event:
    __slots__ = ("seq", "at", "type", "actor", "payload", "prev_hash", "hash")

    def __init__(self, seq, at, type_, actor, payload, prev_hash, hash_=None):
        self.seq = seq
        self.at = at
        self.type = type_
        self.actor = actor
        self.payload = payload
        self.prev_hash = prev_hash
        self.hash = hash_ or digest(
            {"seq": seq, "at": at, "type": type_, "actor": actor,
             "payload": payload, "prev_hash": prev_hash}
        )

    def to_dict(self) -> dict:
        return {"seq": self.seq, "at": self.at, "type": self.type, "actor": self.actor,
                "payload": self.payload, "prev_hash": self.prev_hash, "hash": self.hash}

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(d["seq"], d["at"], d["type"], d["actor"], d["payload"],
                   d["prev_hash"], d["hash"])


class Ledger:
    """JSONL 只追加账本。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[Event] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(Event.from_dict(json.loads(line)))
            self.verify()

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    @property
    def head(self) -> str:
        return self._events[-1].hash if self._events else GENESIS

    def append(self, type_: str, actor: str, payload: dict, at: str | None = None) -> Event:
        ev = Event(len(self._events) + 1, at or now_iso(), type_, actor, payload, self.head)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        self._events.append(ev)
        return ev

    def verify(self) -> None:
        """重算整条链，任何篡改/缺序立即抛出。"""
        prev = GENESIS
        for i, ev in enumerate(self._events):
            if ev.seq != i + 1:
                raise ValueError(f"账本序号断裂: 位置 {i} 读到 seq={ev.seq}")
            if ev.prev_hash != prev:
                raise ValueError(f"哈希链断裂于 seq={ev.seq}")
            expected = digest(
                {"seq": ev.seq, "at": ev.at, "type": ev.type, "actor": ev.actor,
                 "payload": ev.payload, "prev_hash": ev.prev_hash}
            )
            if ev.hash != expected:
                raise ValueError(f"事件内容被篡改: seq={ev.seq}")
            prev = ev.hash
