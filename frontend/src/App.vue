<template>
  <div>
    <h1>MCP Gateway Dashboard</h1>
    <Button label="添加服务" icon="pi pi-plus" @click="showModal = true" />
    <Dialog v-model:visible="showModal" modal header="注册新服务" :style="{ width: '50vw' }">
      <form @submit.prevent="registerService">
        <div class="p-fluid">
          <div class="p-field">
            <label for="name">服务名称</label>
            <InputText id="name" v-model="newService.name" required />
          </div>
          <div class="p-field">
            <label for="description">服务描述</label>
            <InputText id="description" v-model="newService.description" />
          </div>
          <div class="p-field">
            <label for="endpoint">服务端点</label>
            <InputText id="endpoint" v-model="newService.endpoint" required />
          </div>
          <div class="p-field">
            <label for="ip">IP地址</label>
            <InputText id="ip" v-model="newService.ip" />
          </div>
          <div class="p-field">
            <label for="port">端口号</label>
            <InputNumber id="port" v-model="newService.port" />
          </div>
          <Button type="submit" label="注册服务" class="p-mt-3" />
        </div>
      </form>
    </Dialog>
    <div id="services-container">
      <ServiceCard 
        v-for="service in services" 
        :key="service.name"
        :service="service"
      />
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
      port: null
    })
    const showModal = ref(false)

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
          port: null
        }
        showModal.value = false
        fetchServices()
      } catch (error) {
        console.error('Error registering service:', error)
      }
    }

    onMounted(() => {
      fetchServices()
      setInterval(fetchServices, 30000)
    })

    return { services, newService, registerService, showModal }
  }
}
</script>

<style scoped>
#services-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>