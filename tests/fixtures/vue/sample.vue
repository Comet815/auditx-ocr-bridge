<template>
  <view class="wrap">
    <text>{{ title }}</text>
    <view v-for="item in items" :key="item.id">{{ item.name }}</view>
  </view>
</template>

<script setup>
// 坑 1：SFC 的行号必须换算回 .vue 坐标（虚拟源垫行）
// 坑 2：行尾注释紧贴下一个函数（和 JS 同一个坑）
import { ref, computed } from 'vue'
import { fetchItems } from '@/api/items'

const title = ref('列表')  // 行尾注释，紧贴下面的函数
const items = ref([])

function load() {
  return fetchItems().then((r) => {
    items.value = r
  })
}

const total = computed(() => items.value.length)

load()
</script>

<script>
// 坑 3：第二个 script 块。按约定只切第一个，但必须显式告警，不能静默丢弃
export default { name: 'ItemList' }
</script>

<style lang="scss" scoped>
.wrap {
  padding: 12px;
}
</style>
