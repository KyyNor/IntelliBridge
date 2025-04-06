<template>
  <div class="p-4">
    <div class="flex justify-content-between align-items-center mb-4">
      <h1 class="m-0">MCP Gateway Dashboard</h1>
      <Button label="添加服务" icon="pi pi-plus" @click="showModal = true" />
    </div>
    <Dialog v-model:visible="showModal" modal header="注册新服务" :style="{ width: '50vw' }" class="p-fluid" :closeOnEscape="false" :dismissableMask="false">
      <form @submit.prevent="registerService">
        <div class="grid">
          <div class="col-12">
            <div class="field">
              <label for="name" class="block">服务名称</label>
              <InputText id="name" v-model="newService.name" class="w-full" required />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="description" class="block">服务描述</label>
              <InputText id="description" v-model="newService.description" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="endpoint" class="block">服务端点</label>
              <InputText id="endpoint" v-model="newService.endpoint" class="w-full" required />
            </div>
          </div>
          <div class="col-6">
            <div class="field">
              <label for="ip" class="block">IP地址</label>
              <InputText id="ip" v-model="newService.ip" class="w-full" />
            </div>
          </div>
          <div class="col-6">
            <div class="field">
              <label for="port" class="block">端口号</label>
              <InputNumber id="port" v-model="newService.port" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="version" class="block">版本</label>
              <InputText id="version" v-model="newService.version" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <Button type="submit" label="注册服务" class="w-full" />
          </div>
        </div>
      </form>
    </Dialog>
    <Dialog v-model:visible="showEditModal" modal header="修改服务" :style="{ width: '50vw' }" class="p-fluid" :closeOnEscape="false" :dismissableMask="false">
      <form @submit.prevent="updateService">
        <div class="grid">
          <div class="col-12">
            <div class="field">
              <label for="edit-name" class="block">服务名称</label>
              <InputText id="edit-name" v-model="editingService.name" class="w-full" required />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="edit-description" class="block">服务描述</label>
              <InputText id="edit-description" v-model="editingService.description" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="edit-endpoint" class="block">服务端点</label>
              <InputText id="edit-endpoint" v-model="editingService.endpoint" class="w-full" required />
            </div>
          </div>
          <div class="col-6">
            <div class="field">
              <label for="edit-ip" class="block">IP地址</label>
              <InputText id="edit-ip" v-model="editingService.ip" class="w-full" />
            </div>
          </div>
          <div class="col-6">
            <div class="field">
              <label for="edit-port" class="block">端口号</label>
              <InputNumber id="edit-port" v-model="editingService.port" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <div class="field">
              <label for="edit-version" class="block">版本</label>
              <InputText id="edit-version" v-model="editingService.version" class="w-full" />
            </div>
          </div>
          <div class="col-12">
            <Button type="submit" label="保存修改" class="w-full" />
          </div>
        </div>
      </form>
    </Dialog>
    <div id="services-container" class="grid">
      <div class="col-12 md:col-6 lg:col-4" v-for="service in services" :key="service.name">
        <ServiceCard :service="service" @refreshServices="fetchServices" @editService="handleEditService" />
      </div>
    </div>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import axios from 'axios'
import ServiceCard from './components/ServiceCard.vue'
import { API_CONFIG } from './config'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import 'primeicons/primeicons.css'
import 'primeflex/primeflex.css'

export default {
  components: { ServiceCard, Button, Dialog, InputText, InputNumber },
  setup() {
    const services = ref([])
    const newService = ref({
      name: '',
      description: '',
      endpoint: '',
      ip: '',
      port: null,
      version: ''
    })
    const editingService = ref({
      id: '',
      name: '',
      description: '',
      endpoint: '',
      ip: '',
      port: null,
      version: ''
    })
    const showModal = ref(false)
    const showEditModal = ref(false)

    const fetchServices = async () => {
      try {
        const response = await axios.get(`${API_CONFIG.baseUrl}/api/mcp_service_manager/list`)
        services.value = response.data.services
      } catch (error) {
        console.error('Error fetching services:', error)
      }
    }

    const registerService = async () => {
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_manager/register`, newService.value)
        newService.value = {
          name: '',
          description: '',
          endpoint: '',
          ip: '',
          port: null,
          version: ''
        }
        showModal.value = false
        fetchServices()
      } catch (error) {
        console.error('Error registering service:', error)
      }
    }

    const handleEditService = (service) => {
      editingService.value = { ...service }
      showEditModal.value = true
    }

    const updateService = async () => {
      try {
        await axios.post(`${API_CONFIG.baseUrl}/api/mcp_service_manager/register`, editingService.value)
        showEditModal.value = false
        fetchServices()
      } catch (error) {
        console.error('Error updating service:', error)
      }
    }

    onMounted(() => {
      fetchServices()
      setInterval(fetchServices, 30000)
    })

    return { 
      services, 
      newService, 
      editingService,
      registerService, 
      updateService,
      handleEditService,
      showModal,
      showEditModal 
    }
  }
}
</script>

<style scoped>
#services-container {
  gap: 1rem;
  min-height: 100vh;
}
.field {
  margin-bottom: 1.5rem;
}
.field label {
  margin-bottom: 0.5rem;
  font-weight: 500;
}
</style>