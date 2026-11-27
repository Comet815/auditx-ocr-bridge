// 分段器 fixtures（TS）—— 覆盖 TS 特有声明与两类陷阱
// 坑 1：interface / type / enum 也是"可切单元"，不能被当成前导区丢掉
interface Opts {
  retries: number;
  label?: string;
}

type Handler = (s: string) => void;

enum Level {
  Red = 'red',
  Green = 'green',
}

// 坑 2：抽象类里的方法签名没有函数体，按方法切时不能当成语法错误
abstract class Base {
  abstract run(input: string): number;

  protected prefix = 'base';

  // 坑 3：构造器参数属性（private/readonly）必须收进符号表
  constructor(private readonly db: string, public name = 'x') {}

  describe(): string {
    return `${this.prefix}:${this.name}:${this.db}`;
  }
}

// 坑 4：泛型 + 默认参数 + 解构参数
function merge<T extends object>(base: T, extra: Partial<T> = {}, ...rest: T[]): T {
  const out = { ...base, ...extra } as T;
  for (const r of rest) {
    Object.assign(out, r);
  }
  return out;
}

const handler: Handler = (s) => s.trim();

class Impl extends Base {
  run(input: string): number {
    return input.length + this.describe().length;
  }
}

export { Base, Impl, merge, handler, Level };
export type { Opts, Handler };
