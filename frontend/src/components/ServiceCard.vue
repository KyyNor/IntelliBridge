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
        <Button label="查看能力" icon="pi pi-list" class="p-button-primary" @click="showCapabilities" />
        <Button label="修改服务" icon="pi pi-pencil" class="p-button-secondary" @click="editService" />
        <Button label="手动扫描" icon="pi pi-sync" class="p-button-info" @click="scanService" />
        <Button label="删除服务" icon="pi pi-trash" class="p-button-danger" @click="deleteService(service.id)" />
      </div>
    </template>
  </Card>
  
  <!-- 服务能力对话框 -->
  <Dialog v-model:visible="capabilitiesDialog" modal header="服务能力" :style="{ width: '70vw' }" class="p-fluid">
    <div v-if="loading" class="flex justify-content-center">
      <ProgressSpinner />
    </div>
    <div v-else-if="capabilities.length === 0" class="text-center p-4">
      <i class="pi pi-info-circle text-3xl text-primary mb-3"></i>
      <p>该服务暂无能力信息，请先进行服务扫描</p>
      <Button label="扫描服务" icon="pi pi-sync" class="p-button-info" @click="scanAndGetCapabilities" />
    </div>
    <TabView v-else>
      <TabPanel header="工具">
        <Accordion :multiple="true" class="capability-accordion">
          <AccordionTab v-for="cap in toolCapabilities" :key="cap.id" :header="cap.name">
            <div class="grid">
              <div class="col-12">
                <div class="field">
                  <label class="block text-sm font-medium text-500 mb-1">描述</label>
                  <div class="text-900">{{ cap.description || 'N/A' }}</div>
                </div>
              </div>
              <div class="col-12" v-if="cap.parameters">
                <div class="field">
                  <label class="block text-sm font-medium text-500 mb-1">参数</label>
                  <div class="text-900 parameters-container">
                    <pre>{{ formatParameters(cap.parameters) }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </AccordionTab>
        </Accordion>
      </TabPanel>
      <TabPanel header="资源">
        <Accordion :multiple="true" class="capability-accordion">
          <AccordionTab v-for="cap in resourceCapabilities" :key="cap.id" :header="cap.name">
            <div class="grid">
              <div class="col-12">
                <div class="field">
                  <label class="block text-sm font-medium text-500 mb-1">描述</label>
                  <div class="text-900">{{ cap.description || 'N/A' }}</div>
                </div>
              </div>
              <div class="col-12" v-if="cap.parameters">
                <div class="field">
                  <label class="block text-sm font-medium text-500 mb-1">参数</label>
                  <div class="text-900 parameters-container">
                    <pre>{{ formatParameters(cap.parameters) }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </AccordionTab>
        </Accordion>
      </TabPanel>
    </TabView>
  </Dialog>
</template>

<script>
import axios from 'axios'
import { ref } from 'vue'
import { API_CONFIG } from '../config'
import Card from 'primevue/card'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import TabView from 'primevue/tabview'
import TabPanel from 'primevue/tabpanel'
import Accordion from 'primevue/accordion'
import AccordionTab from 'primevue/accordiontab'
import ProgressSpinner from 'primevue/progressspinner'

export default {
  components: { 
    Card, Button, Tag, Dialog, TabView, TabPanel, 
    Accordion, AccordionTab, ProgressSpinner 
  },
  props: {
    service: {
      type: Object,
      required: true
    }
  },
  setup() {
    const capabilitiesDialog = ref(false)
    const capabilities = ref([])
    const loading = ref(false)

    return {
      capabilitiesDialog,
      capabilities,
      loading
    }
  },
  computed: {
    toolCapabilities() {
      return this.capabilities.filter(cap => cap.cap_type === 'tool')
    },
    resourceCapabilities() {
      return this.capabilities.filter(cap => cap.cap_type === 'resource')
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
      // 确保传递完整的服务信息，包括id字段
      this.$emit('editService', this.service)
    },
    formatDateTime(datetime) {
      if (!datetime) return 'N/A'
      return new Date(datetime).toLocaleString()
    },
    getStatusSeverity(status) {
      const severityMap = {
        'online': 'success',
        'offline': 'danger',
        'maintenance': 'warning'
      }
      return severityMap[status] || 'info'
    },
    async showCapabilities() {
      this.capabilitiesDialog = true
      await this.fetchCapabilities()
    },
    async fetchCapabilities() {
      this.loading = true
      try {
        const response = await axios.get(`${API_CONFIG.baseUrl}/api/mcp_service_manager/capabilities/${this.service.id}`)
        this.capabilities = response.data.capabilities || []
      } catch (error) {
        console.error('Error fetching capabilities:', error)
      } finally {
        this.loading = false
      }
    },
    async scanAndGetCapabilities() {
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_scanner/scan/${this.service.id}`)
        await this.fetchCapabilities()
        this.$emit('refreshServices')
      } catch (error) {
        console.error('Error scanning service:', error)
      }
    },
    formatParameters(parameters) {
      if (!parameters) return 'N/A'
      try {
        const parsed = JSON.parse(parameters)
        return JSON.stringify(parsed, null, 2)
      } catch (e) {
        return parameters
      }
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
.capability-accordion {
  margin-top: 1rem;
}
.parameters-container {
  background-color: #f8f9fa;
  padding: 0.5rem;
  border-radius: 4px;
  max-height: 300px;
  overflow-y: auto;
}
.parameters-container pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>