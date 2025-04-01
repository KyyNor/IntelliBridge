<template>
  <Card class="shadow-3 m-3">
    <template #header>
      <div class="flex justify-content-between align-items-center p-3">
        <h3 class="m-0">{{ service.name }}</h3>
        <Tag :value="service.status" 
             :severity="getStatusSeverity(service.status)" />
      </div>
    </template>
    <template #content>
      <div class="grid">
        <div class="col-12">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">服务描述</label>
            <div class="text-900">{{ service.description || 'N/A' }}</div>
          </div>
        </div>
        <div class="col-12">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">服务端点</label>
            <div class="text-900">{{ service.endpoint }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">IP地址</label>
            <div class="text-900">{{ service.ip || 'N/A' }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">端口号</label>
            <div class="text-900">{{ service.port || 'N/A' }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">版本</label>
            <div class="text-900">{{ service.version || 'N/A' }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">创建方式</label>
            <div class="text-900">{{ service.creation_type === 'manual' ? '手动' : '自动' }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">创建时间</label>
            <div class="text-900">{{ formatDateTime(service.created_tm) }}</div>
          </div>
        </div>
        <div class="col-6">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">修改时间</label>
            <div class="text-900">{{ formatDateTime(service.updated_tm) }}</div>
          </div>
        </div>
        <div class="col-12">
          <div class="field">
            <label class="block text-sm font-medium text-500 mb-1">状态检查时间</label>
            <div class="text-900">{{ formatDateTime(service.status_check_tm) }}</div>
          </div>
        </div>
      </div>
    </template>
    <template #footer>
      <div class="flex justify-content-end gap-2">
        <Button label="修改服务" icon="pi pi-pencil" class="p-button-secondary" @click="editService" />
        <Button label="手动扫描" icon="pi pi-sync" class="p-button-info" @click="scanService" />
        <Button label="删除服务" icon="pi pi-trash" class="p-button-danger" @click="deleteService(service.id)" />
      </div>
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
    async scanService() {
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_scanner/scan/${this.service.id}`)
        this.$emit('refreshServices')
      } catch (error) {
        console.error('Error scanning service:', error)
      }
    },
    editService() {
      this.$emit('editService', this.service)
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
.field {
  margin-bottom: 1rem;
}
.field:last-child {
  margin-bottom: 0;
}
</style>