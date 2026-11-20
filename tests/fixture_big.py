"""测试用大文件：造一个超长类 + 若干顶层函数，用来验证分段器。

刻意包含：
- 一个超过 max_lines 的大类（会被按方法切开）
- 定义在类里、但被顶层函数引用的名字（模拟跨块引用）
- 常量区（应被补进每一块）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_ITEMS = 100
DEFAULT_NAME = "fixture"
SEVERITY = ("low", "medium", "high")


def load_config(path):
    """读配置——故意用 Path，测试前导区是否被补进每一块。"""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def normalize_name(raw):
    name = (raw or DEFAULT_NAME).strip().lower()
    return "_".join(name.split())


def pick_severity(score):
    if score >= 80:
        return SEVERITY[2]
    if score >= 50:
        return SEVERITY[1]
    return SEVERITY[0]


def describe(env_keys):
    """用 os.environ——第二处前导区依赖。"""
    return ", ".join(sorted(env_keys)) if env_keys else "<none>"


class Ledger:
    """一个很大的类，方法数足够多，分段器应把它按方法切开。"""

    def __init__(self, root, name=None):
        self.root = Path(root)
        self.name = normalize_name(name)
        self.items = []
        self._seq = 0
        self.config = load_config(self.root / "cfg.json")

    def append(self, item):
        self._seq += 1
        self.items.append(dict(item, seq=self._seq))
        return self._seq

    def extend(self, items):
        for it in items:
            self.append(it)
        return len(self.items)

    def find(self, key, value):
        return [it for it in self.items if it.get(key) == value]

    def first(self, key):
        hit = self.find("key", key)
        return hit[0] if hit else None

    def remove(self, seq):
        before = len(self.items)
        self.items = [it for it in self.items if it.get("seq") != seq]
        return before - len(self.items)

    def clear(self):
        n = len(self.items)
        self.items = []
        self._seq = 0
        return n

    def count(self):
        return len(self.items)

    def summarize(self):
        buckets = {}
        for it in self.items:
            k = it.get("kind", "unknown")
            buckets[k] = buckets.get(k, 0) + 1
        return buckets

    def top(self, n=5):
        ranked = sorted(self.items, key=lambda it: it.get("score", 0), reverse=True)
        return ranked[:n]

    def score_of(self, key):
        it = self.first(key)
        return it.get("score", 0) if it else 0

    def label_of(self, key):
        it = self.first(key)
        return it.get("label", "") if it else ""

    def promote(self, seq):
        for it in self.items:
            if it.get("seq") == seq:
                it["kind"] = "promoted"
                return True
        return False

    def demote(self, seq):
        for it in self.items:
            if it.get("seq") == seq:
                it["kind"] = "demoted"
                return True
        return False

    def rename(self, seq, new_label):
        for it in self.items:
            if it.get("seq") == seq:
                it["label"] = new_label
                return True
        return False

    def to_json(self):
        return json.dumps({"name": self.name, "items": self.items},
                          ensure_ascii=False, indent=2)

    def save(self):
        target = self.root / f"{self.name}.json"
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    def load(self):
        target = self.root / f"{self.name}.json"
        if not target.exists():
            return 0
        data = json.loads(target.read_text(encoding="utf-8"))
        self.items = data.get("items", [])
        self._seq = max([it.get("seq", 0) for it in self.items] or [0])
        return len(self.items)

    def env_report(self):
        return describe(os.environ.keys())

    def stats(self):
        scores = [it.get("score", 0) for it in self.items]
        if not scores:
            return {"n": 0, "min": 0, "max": 0, "avg": 0}
        return {"n": len(scores), "min": min(scores), "max": max(scores),
                "avg": sum(scores) / len(scores)}

    def cluster(self, key="kind"):
        out = {}
        for it in self.items:
            out.setdefault(it.get(key, "unknown"), []).append(it.get("label"))
        return out

    def prune(self, threshold=0):
        keep = [it for it in self.items if it.get("score", 0) >= threshold]
        dropped = len(self.items) - len(keep)
        self.items = keep
        return dropped

    def merge_from(self, other):
        for it in other.items:
            self.append(it)
        return len(self.items)

    def keys(self):
        seen = []
        for it in self.items:
            k = it.get("key")
            if k and k not in seen:
                seen.append(k)
        return seen

    def is_empty(self):
        return not self.items

    def capacity(self):
        return max(0, MAX_ITEMS - len(self.items))


def build_ledger(root, name=None):
    """顶层函数引用类内的 normalize_name——跨块引用测试点。"""
    led = Ledger(root, name)
    led.append({"key": "a", "kind": "bug", "score": 90, "label": normalize_name("A Bug")})
    led.append({"key": "b", "kind": "warn", "score": 40, "label": normalize_name("B Warn")})
    led.append({"key": "c", "kind": "info", "score": 10, "label": normalize_name("C Info")})
    return led


def run_report(root):
    led = build_ledger(root)
    return {
        "summary": led.summarize(),
        "stats": led.stats(),
        "top": led.top(2),
        "severity": pick_severity(led.score_of("a")),
        "env": led.env_report(),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(run_report(sys.argv[1] if len(sys.argv) > 1 else "."),
                     ensure_ascii=False, indent=2))
