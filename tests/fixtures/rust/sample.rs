// 分段器 fixtures（Rust）—— 覆盖 Rust 特有的三个坑
use std::collections::HashMap;

// 坑 1：前导区是 use + 顶层 const/static（注意与 Go 相反，没有 package 子句）
const MAX_ITEMS: usize = 100;
static NAME: &str = "fixture";

// 坑 2：属性是独立兄弟节点（attribute_item），必须并进被修饰项的起点，
// 否则 struct 的 #[derive] 会掉进前导区、或者被上一块带走。
#[derive(Debug, Clone)]
pub struct Store {
    items: Vec<String>,
    index: HashMap<String, usize>,
}

impl Store {
    // 坑 3：impl 块按成员切开时要补 `impl T {` 头**和**收尾 `}`
    pub fn new() -> Self {
        Store {
            items: Vec::new(),
            index: HashMap::new(),
        }
    }

    pub fn add(&mut self, item: String) -> Result<(), String> {
        if self.items.len() >= MAX_ITEMS {
            return Err("too many".to_string());
        }
        self.index.insert(item.clone(), self.items.len());
        self.items.push(item);
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }
}

// 坑 4：trait 与 trait impl 也是可切单元
pub trait Loader {
    fn load(&self, raw: &str) -> usize;
}

impl Loader for Store {
    fn load(&self, raw: &str) -> usize {
        raw.len() + self.len()
    }
}

pub fn make(count: usize) -> Vec<String> {
    (0..count).map(|i| format!("{NAME}-{i}")).collect()
}
