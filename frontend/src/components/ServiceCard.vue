<template>
  <Card class="m-2">
    <template #header>
      <h3>{{ service.name }}</h3>
    </template>
    <template #content>
      <p>Endpoint: {{ service.endpoint }}</p>
      <p>IP: {{ service.ip }}</p>
      <p>Port: {{ service.port }}</p>
      <p>Version: {{ service.version || 'N/A' }}</p>
      <p>Status: 
        <Tag :value="service.status" 
             :severity="getStatusSeverity(service.status)" />
      </p>
      <p>创建时间: {{ formatDateTime(service.created_tm) }}</p>
      <p>修改时间: {{ formatDateTime(service.updated_tm) }}</p>
      <p>状态检查时间: {{ formatDateTime(service.status_check_tm) }}</p>
      <p>创建方式: {{ service.creation_type === 'manual' ? '手动' : '自动' }}</p>
    </template>
    <template #footer>
      <Button label="删除服务" icon="pi pi-trash" class="p-button-danger" @click="deleteService(service.id)" />
    </template>
  </Card>
</template>

<script>
import axios from 'axios'
import { API_CONFIG } from '../config'
import Card from 'primevue/card'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

export default {
  components: { Card, Button, Tag },
  props: {
    service: {
      type: Object,
      required: true
    }
  },
  methods: {
    async deleteService(serviceId) {
      try {
        await axios.delete(`${API_CONFIG.baseUrl}/api/mcp_service_manager/delete/${serviceId}`)
        this.$emit('refreshServices')
      } catch (error) {
        console.error('Error deleting service:', error)
      }
    },
    formatDateTime(datetime) {
      if (!datetime) return 'N/A'
      return new Date(datetime).toLocaleString()
    },
    getStatusSeverity(status) {
      const severityMap = {
        'active': 'success',
        'offline': 'danger',
        'maintenance': 'warning'
      }
      return severityMap[status] || 'info'
    }
  }
}
</script>

<style scoped>
.p-card-content p {
  margin: 0.5rem 0;
}
</style>