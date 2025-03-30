<template>
  <Card class="m-2">
    <template #header>
      <h3>{{ service.name }}</h3>
    </template>
    <template #content>
      <p>Endpoint: {{ service.endpoint }}</p>
      <p>Status: 
        <Tag :value="service.healthy ? 'Healthy' : 'Unhealthy'" 
             :severity="service.healthy ? 'success' : 'danger'" />
      </p>
      <p>Version: {{ service.version || 'N/A' }}</p>
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
        // 假设父组件有刷新服务列表的方法
        this.$emit('refreshServices')
      } catch (error) {
        console.error('Error deleting service:', error)
      }
    }
  }
 }
</script>

<style scoped>

</style>