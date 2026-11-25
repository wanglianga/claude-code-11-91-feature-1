<template>
  <header class="app-header">
    <h1>⚡ 充电预约演示系统</h1>
    <nav>
      <RouterLink to="/driver">司机端</RouterLink>
      <RouterLink to="/fleet">车队批次</RouterLink>
      <RouterLink to="/operations">运营端</RouterLink>
    </nav>
  </header>
  <main>
    <RouterView />
  </main>
</template>

<script setup></script>
