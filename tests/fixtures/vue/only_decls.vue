<template>
  <view>{{ label }}</view>
</template>

<script setup>
// 坑 4：script 里只有 import 与常量，没有任何函数/类 ——
// 旧逻辑切出 0 块，文件静默逃过审查（2026-11-27 由昆仑 App.vue 暴露）
import { ref } from 'vue'
import { DEFAULTS } from '@/config'

const label = ref(DEFAULTS.label)
const count = DEFAULTS.count
</script>

<style>
view {
  font-size: 14px;
}
</style>
