"""分段器 fixtures（Python）—— 覆盖装饰器边界

坑 1：Python 3.8+ 的 FunctionDef.lineno 指向 def 行、不含装饰器。
      第一个顶层函数的装饰器曾整行掉进前导区，随前导区补进每一块。
坑 2：类被按方法切开时，方法自己的装饰器（@property）曾被切出函数体丢失。
"""
from __future__ import annotations

import functools
import json

MAX_ITEMS = 100


@functools.lru_cache(maxsize=8)
def cached_load(path):
    # 坑 1：这个装饰器必须跟着函数走，不能留在前导区
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def summarize(items):
    return {"count": len(items), "max": max(items) if items else 0}


class Store:
    def __init__(self):
        self._items = []

    def add(self, item):
        if len(self._items) >= MAX_ITEMS:
            raise ValueError("too many items")
        self._items.append(item)

    @property
    def size(self):
        # 坑 2：这个装饰器按方法切开时不能丢
        return len(self._items)

    def reset(self):
        self._items = []
