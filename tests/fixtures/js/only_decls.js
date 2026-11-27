// 坑 6：整个文件只有 import 与常量，没有任何可切单元。
// 旧逻辑会切出 0 块 —— 文件静默逃过审查（2026-11-27 真实数据暴露）。
import path from 'path';
import { readFileSync } from 'fs';

const ROOT = '/tmp/auditx';
const LIMIT = 10;
const EXT = path.extname('a.js');

export { ROOT, LIMIT, EXT, readFileSync };
