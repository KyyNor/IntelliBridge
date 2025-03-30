<template>
  <div :class="['service-card', service.healthy ? 'healthy' : 'unhealthy']">
    <h3>{{ service.name }}</h3>
    <p>Endpoint: {{ service.endpoint }}</p>
    <p>Status: {{ service.healthy ? 'Healthy' : 'Unhealthy' }}</p>
    <p>Version: {{ service.version || 'N/A' }}</p>
  </div>
</template>

<script>
export default {
  props: {
    service: {
      type: Object,
      required: true
    }
  }
}
</script>

<style scoped>
.service-card {
  border: 1px solid #ddd;
  border-radius: 5px;
  padding: 15px;
  margin-bottom: 10px;
}

.healthy {
  background-color: #e6ffe6;
}

.unhealthy {
  background-color: #ffe6e6;
}
</style>