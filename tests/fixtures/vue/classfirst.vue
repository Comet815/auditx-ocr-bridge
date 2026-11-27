<template>
  <view>{{ total }}</view>
</template>

<script setup>
// 坑 7（F13）Vue 变体：脚本里第一个可切单元是类，且类体超 max-lines 被拆方法。
// chunk_vue 委托 chunk_js/chunk_ts，所以修在那边就自动痊愈——这条样本盯住委托关系。
class Counter {
  constructor() {
    this.n = 0;
  }

  inc() {
    this.n += 1;
    return this.n;
  }

  dec() {
    this.n -= 1;
    return this.n;
  }

  reset() {
    this.n = 0;
  }

  value() {
    return this.n;
  }
}

const counter = new Counter();
const total = counter.value();
</script>
