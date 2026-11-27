// 坑 7（F13）TS 变体：`export class` 在最前，且类体超 max-lines 被拆方法。
// 前导区基准必须取 export 行本身，而不是第一个方法的行。
export class Registry {
  private entries: string[] = [];

  register(name: string): number {
    this.entries.push(name);
    return this.entries.length;
  }

  unregister(name: string): number {
    this.entries = this.entries.filter((e) => e !== name);
    return this.entries.length;
  }

  list(): string[] {
    return [...this.entries];
  }

  size(): number {
    return this.entries.length;
  }
}

export function make(): Registry {
  return new Registry();
}
