<template>
  <div class="card">
    <div class="flex justify-content-between align-items-center mb-3">
      <h3 class="m-0">服务列表</h3>
      <div class="flex align-items-center">
        <Select v-model="rowsPerPage" :options="rowsPerPageOptions" optionLabel="label" 
                  optionValue="value" class="mr-2" placeholder="每页显示" />
        <span class="text-sm text-500">共 {{ totalRecords }} 个服务</span>
      </div>
    </div>
    
    <DataTable :value="services" :paginator="true" :rows="rowsPerPage" 
               :totalRecords="totalRecords" 
               paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink CurrentPageReport"
               currentPageReportTemplate="{first} - {last} / {totalRecords}"
               responsiveLayout="scroll" stripedRows 
               :rowsPerPageOptions="[10, 20, 50]" 
               v-model:filters="filters" filterDisplay="menu"
               class="p-datatable-sm">
      
      <Column field="name" header="服务名称" :sortable="true">
        <template #filter="{filterModel, filterCallback}">
          <InputText v-model="filterModel.value" @input="filterCallback()" placeholder="搜索名称" class="p-column-filter" />
        </template>
      </Column>
      
      <Column field="endpoint" header="服务端点" :sortable="true">
        <template #filter="{filterModel, filterCallback}">
          <InputText v-model="filterModel.value" @input="filterCallback()" placeholder="搜索端点" class="p-column-filter" />
        </template>
      </Column>
      
      <Column field="status" header="状态" :sortable="true">
        <template #body="{data}">
          <Tag :value="data.status" :severity="getStatusSeverity(data.status)" />
        </template>
        <template #filter="{filterModel, filterCallback}">
          <Select v-model="filterModel.value" @change="filterCallback()" :options="statusOptions" 
                    placeholder="选择状态" class="p-column-filter" />
        </template>
      </Column>
      
      <Column field="version" header="版本" :sortable="true">
        <template #body="{data}">
          {{ data.version || 'N/A' }}
        </template>
      </Column>
      
      <Column field="updated_tm" header="更新时间" :sortable="true">
        <template #body="{data}">
          {{ formatDateTime(data.updated_tm) }}
        </template>
      </Column>
      
      <Column header="操作" :exportable="false" style="min-width: 8rem">
        <template #body="{data}">
          <div class="flex justify-content-center gap-2">
            <Button v-tooltip.top="'查看能力'" icon="pi pi-list" 
                    class="p-button-primary p-button-sm p-button-rounded" 
                    @click="showCapabilities(data)">
            </Button>
            <Button v-tooltip.top="'测试能力'" icon="pi pi-play" 
                    class="p-button-success p-button-sm p-button-rounded" 
                    @click="showCapabilityTester(data)">
            </Button>
            <Button v-tooltip.top="'修改服务'" icon="pi pi-pencil" 
                    class="p-button-secondary p-button-sm p-button-rounded" 
                    @click="editService(data)">
            </Button>
            <Button v-tooltip.top="'手动扫描'" icon="pi pi-sync" 
                    class="p-button-info p-button-sm p-button-rounded" 
                    @click="scanService(data.id)">
            </Button>
            <Button v-tooltip.top="'删除服务'" icon="pi pi-trash" 
                    class="p-button-danger p-button-sm p-button-rounded" 
                    @click="deleteService(data.id)">
            </Button>
          </div>
        </template>
      </Column>
    </DataTable>
  </div>
  
  <!-- 服务能力对话框 -->
  <Dialog v-model:visible="capabilitiesDialog" modal header="服务能力" :style="{ width: '70vw' }" class="p-fluid" :closeOnEscape="true" :dismissableMask="true">
    <div v-if="loading" class="flex justify-content-center">
      <ProgressSpinner />
    </div>
    <div v-else-if="capabilities.length === 0" class="text-center p-4">
      <i class="pi pi-info-circle text-3xl text-primary mb-3"></i>
      <p>该服务暂无能力信息，请先进行服务扫描</p>
      <Button label="扫描服务" icon="pi pi-sync" class="p-button-info p-button-rounded" @click="scanAndGetCapabilities" />
    </div>
    <Tabs value="0">
      <TabList>
        <Tab value="0">工具</Tab>
        <Tab value="1">资源</Tab>
      </TabList>
      <TabPanels>
        <TabPanel value="0">
            <Accordion value="0" class="capability-accordion">
            <AccordionPanel v-for="(cap, index) in toolCapabilities" :key="cap.name" :value="cap.id">
                <AccordionHeader>{{cap.name}}</AccordionHeader>
                <AccordionContent>
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
                </AccordionContent>
            </AccordionPanel>
            </Accordion>
        </TabPanel>
        <TabPanel value="1">
            <Accordion :multiple="true" class="capability-accordion">
            <AccordionPanel v-for="(cap, index) in resourceCapabilities" :key="cap.name" :value="cap.id">
              <AccordionHeader>{{cap.name}}</AccordionHeader>
              <AccordionContent>
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
              </AccordionContent>
            </AccordionPanel>
            </Accordion>
        </TabPanel>
      </TabPanels>
    </Tabs>
  </Dialog>
  
  <!-- 删除确认对话框 -->
  <Dialog v-model:visible="deleteConfirmDialog" modal header="确认删除" :style="{ width: '25vw' }" class="p-fluid delete-confirm-dialog">
    <div class="p-3 text-center">
      <i class="pi pi-exclamation-triangle text-4xl text-yellow-500 mb-2"></i>
      <p class="font-bold mb-2">确定要删除此服务吗？</p>
      <div class="service-info p-2 mb-2" v-if="selectedService">
        <div class="info-item"><span class="info-label">服务名称:</span> {{ selectedService.name }}</div>
        <div class="info-item"><span class="info-label">服务端点:</span> {{ selectedService.endpoint }}</div>
      </div>
      <p class="text-sm text-red-500">此操作不可恢复</p>
    </div>
    <template #footer>
      <div class="flex justify-content-end gap-2">
        <Button label="取消" icon="pi pi-times" class="p-button-text p-button-sm" @click="deleteConfirmDialog = false" />
        <Button label="确认删除" icon="pi pi-trash" class="p-button-danger p-button-sm" @click="confirmDelete" />
      </div>
    </template>
  </Dialog>
  
  <!-- 服务能力测试对话框 -->
  <CapabilityTester v-model:visible="testerVisible" :serviceId="selectedService?.id" />
</template>

<script>
import axios from 'axios'
import { ref, computed, onMounted, watch } from 'vue'
import { API_CONFIG } from '../config'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import TabPanels from 'primevue/tabpanels';
import TabPanel from 'primevue/tabpanel';
import Accordion from 'primevue/accordion'
import AccordionPanel from 'primevue/accordionpanel'
import AccordionHeader from 'primevue/accordionheader'
import AccordionContent from 'primevue/accordioncontent'
import ProgressSpinner from 'primevue/progressspinner'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'
import CapabilityTester from './CapabilityTester.vue'

export default {
  components: {
    DataTable, Column, Button, Tag, Dialog, Tabs, TabList, Tab, TabPanels, TabPanel,
    Accordion, AccordionPanel, AccordionHeader, AccordionContent, ProgressSpinner, 
    Select, InputText, CapabilityTester
  },
  props: {
    services: {
      type: Array,
      required: true
    }
  },
  emits: ['refreshServices', 'editService'],
  setup(props, { emit }) {
    const rowsPerPageOptions = ref([10, 20, 50])
    const rowsPerPage = ref(10)
    const totalRecords = ref(0)
    const filters = ref({})
    const statusOptions = ref(['online', 'offline', 'maintenance'])
    const capabilitiesDialog = ref(false)
    const capabilities = ref([])
    const loading = ref(false)
    const deleteConfirmDialog = ref(false)
    const serviceToDelete = ref(null)
    const testerVisible = ref(false)
    const selectedService = ref(null)
    const currentService = ref(null)
    
    // 计算属性
    const toolCapabilities = computed(() => {
      return capabilities.value.filter(cap => cap.cap_type === 'tool')
    })
    
    const resourceCapabilities = computed(() => {
      return capabilities.value.filter(cap => cap.cap_type === 'resource')
    })
    
    // 监听服务列表变化，更新总记录数
    watch(() => props.services, (newServices) => {
      totalRecords.value = newServices.length
    }, { immediate: true })
    
    // 方法
    const deleteService = (serviceId) => {
      serviceToDelete.value = serviceId
      selectedService.value = props.services.find(s => s.id === serviceId)
      deleteConfirmDialog.value = true
    }
    
    const confirmDelete = async () => {
      try {
        await axios.delete(`${API_CONFIG.baseUrl}/api/mcp_service_manager/delete/${serviceToDelete.value}`)
        deleteConfirmDialog.value = false
        emit('refreshServices')
      } catch (error) {
        console.error('Error deleting service:', error)
      }
    }
    
    const scanService = async (serviceId) => {
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_scanner/scan/${serviceId}`)
        emit('refreshServices')
      } catch (error) {
        console.error('Error scanning service:', error)
      }
    }
    
    const editService = (service) => {
      emit('editService', service)
    }
    
    const formatDateTime = (datetime) => {
      if (!datetime) return 'N/A'
      return new Date(datetime).toLocaleString()
    }
    
    const getStatusSeverity = (status) => {
      const severityMap = {
        'online': 'success',
        'offline': 'danger',
        'maintenance': 'warning'
      }
      return severityMap[status] || 'info'
    }
    
    const showCapabilities = (service) => {
      currentService.value = service
      capabilitiesDialog.value = true
      fetchCapabilities(service.id)
    }
    
    const showCapabilityTester = (service) => {
      selectedService.value = service
      testerVisible.value = true
    }
    
    const fetchCapabilities = async (serviceId) => {
      loading.value = true
      try {
        const response = await axios.get(`${API_CONFIG.baseUrl}/api/mcp_service_manager/capabilities/${serviceId}`)
        capabilities.value = response.data.capabilities || []
      } catch (error) {
        console.error('Error fetching capabilities:', error)
      } finally {
        loading.value = false
      }
    }
    
    const scanAndGetCapabilities = async () => {
      if (!currentService.value) return
      
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_scanner/scan/${currentService.value.id}`)
        await fetchCapabilities(currentService.value.id)
        emit('refreshServices')
      } catch (error) {
        console.error('Error scanning service:', error)
      }
    }
    
    const formatParameters = (parameters) => {
      if (!parameters) return 'N/A'
      try {
        const parsed = JSON.parse(parameters)
        return JSON.stringify(parsed, null, 2)
      } catch (e) {
        return parameters
      }
    }
    
    return {
      rowsPerPageOptions,
      rowsPerPage,
      totalRecords,
      filters,
      statusOptions,
      capabilitiesDialog,
      capabilities,
      loading,
      deleteConfirmDialog,
      serviceToDelete,
      testerVisible,
      selectedService,
      currentService,
      toolCapabilities,
      resourceCapabilities,
      deleteService,
      confirmDelete,
      scanService,
      editService,
      formatDateTime,
      getStatusSeverity,
      showCapabilities,
      showCapabilityTester,
      fetchCapabilities,
      scanAndGetCapabilities,
      formatParameters
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
.delete-confirm-dialog :deep(.p-dialog-content) {
  padding: 0.75rem;
}
.service-info {
  background-color: #f8f9fa;
  border-radius: 4px;
  text-align: left;
  border: 1px solid #e9ecef;
}
.info-item {
  padding: 0.25rem 0;
}
.info-label {
  font-weight: 600;
  margin-right: 0.5rem;
}
</style>