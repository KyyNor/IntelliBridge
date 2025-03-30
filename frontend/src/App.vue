<template>
  <div>
    <h1>MCP Gateway Dashboard</h1>
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

export default {
  components: { ServiceCard },
  setup() {
    const services = ref([])

    const fetchServices = async () => {
      try {
        const response = await axios.get('/api/services/list')
        services.value = response.data.services
      } catch (error) {
        console.error('Error fetching services:', error)
      }
    }

    onMounted(() => {
      fetchServices()
      setInterval(fetchServices, 30000)
    })

    return { services }
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