// 分段器 fixtures（JS）—— 专挑踩过的坑，不是随便写的样本
// 坑 1：行尾注释紧贴下一个函数（曾被并进函数起点，导致前导区丢这条常量）
import fs from 'fs';
const MAX = 3;  // 行尾注释，紧贴下一个函数
const CONFIG = { retries: 1 };

/**
 * 坑 2：JSDoc 前导注释必须跟着函数走，不能被前导区或上一块带走
 */
function alpha(x) {
  if (x > MAX) {
    return CONFIG.retries;
  }
  return x;
}

// 坑 3：箭头函数常量也算可切单元
const beta = (y) => alpha(y) + 1;

// 坑 4：类被按方法切开时要补 `class X {` 头**和**收尾 `}`
class Worker {
  constructor(name) {
    this.name = name;
  }

  run() {
    return beta(MAX);
  }

  static create() {
    return new Worker('w');
  }
}

// 坑 5：export 包着的声明也要能切
export default Worker;
export { alpha, beta, MAX };
