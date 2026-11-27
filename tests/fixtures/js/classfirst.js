// 坑 7（F13）：类是文件**第一个**可切单元，且类体超过 max-lines 被按方法拆开。
// 旧逻辑 preamble_end = min(items 起点) − 1，类被拆时这个最小值是**第一个方法**的行，
// 于是 `class Store {` 整行掉进前导区、被逐字补进每一块：
// 方法块出现双 class 头、顶层块挂着未闭合的 class，所有块解析失败。
// 症状：0 块可用、exit 1（fail-closed，没有静默损坏）。
// 注意：类体必须真的超过 max-lines（CI 用 20），否则整类成一块，踩不到这个坑。
class Store {
  constructor() {
    this.items = [];
    this.index = {};
  }

  add(x) {
    this.index[x] = this.items.length;
    this.items.push(x);
    return this.items.length;
  }

  remove(x) {
    const at = this.index[x];
    if (at === undefined) {
      return this.items.length;
    }
    this.items.splice(at, 1);
    return this.items.length;
  }

  size() {
    return this.items.length;
  }

  clear() {
    this.items = [];
    this.index = {};
  }

  snapshot() {
    return {
      count: this.items.length,
      items: [...this.items],
    };
  }
}

function helper() {
  return 1;
}
