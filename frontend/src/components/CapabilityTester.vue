<template>
  <Dialog v-model:visible="visible" modal header="测试MCP能力" :style="{ width: '60vw' }" class="p-fluid" :closeOnEscape="true" :dismissableMask="true">
    <div class="grid">
      <div class="col-12" v-if="loading">
        <div class="flex justify-content-center">
          <ProgressSpinner />
        </div>
      </div>
      <div class="col-12" v-else>
        <div class="field">
          <label for="capability-select" class="block text-sm font-medium text-500 mb-1">选择能力</label>
          <Select id="capability-select" v-model="selectedCapability" :options="capabilities" 
                  optionLabel="name" class="w-full" placeholder="选择要测试的能力">
            <template #option="slotProps">
              <div class="flex align-items-center">
                <span :class="{'text-blue-500': slotProps.option.cap_type === 'tool', 'text-green-500': slotProps.option.cap_type === 'resource'}">{{ slotProps.option.cap_type === 'tool' ? '🔧' : '📦' }}</span>
                <span class="ml-2">{{ slotProps.option.name }}</span>
              </div>
            </template>
          </Select>
        </div>
        
        <div class="field mt-3" v-if="selectedCapability && selectedCapability.cap_type === 'tool' && parametersSchema">
          <label class="block text-sm font-medium text-500 mb-1">参数</label>
          <div v-for="(param, key) in parametersSchema.properties" :key="key" class="mb-2">
            <label :for="key" class="block text-xs font-medium text-500 mb-1">{{ param.title || key }} <span v-if="isRequired(key)" class="text-red-500">*</span></label>
            <InputText v-if="param.type === 'string'" :id="key" v-model="parameters[key]" class="w-full" />
            <InputNumber v-else-if="param.type === 'number' || param.type === 'integer'" :id="key" v-model="parameters[key]" class="w-full" />
            <Checkbox v-else-if="param.type === 'boolean'" :id="key" v-model="parameters[key]" :binary="true" />
            <Textarea v-else-if="param.type === 'object'" :id="key" v-model="parameters[key]" rows="5" class="w-full" placeholder="输入JSON对象" />
            <small v-if="param.description" class="block text-xs text-500 mt-1">{{ param.description }}</small>
          </div>
        </div>
        
        <div class="field mt-3" v-if="selectedCapability">
          <label class="block text-sm font-medium text-500 mb-1">能力描述</label>
          <div class="p-2 border-1 border-round surface-100">{{ selectedCapability.description || '无描述' }}</div>
        </div>
        
        <div class="field mt-3" v-if="testResult">
          <label class="block text-sm font-medium text-500 mb-1">测试结果</label>
          <div class="p-2 border-1 border-round" :class="{'surface-200': !testResult.success, 'surface-100': testResult.success}">
            <div v-if="testResult.error" class="text-red-500">{{ testResult.error }}</div>
            <pre v-else class="m-0 overflow-auto" style="max-height: 300px">{{ JSON.stringify(testResult.result, null, 2) }}</pre>
          </div>
        </div>
      </div>
    </div>
    
    <template #footer>
      <div class="flex justify-content-end gap-2">
        <Button label="关闭" icon="pi pi-times" class="p-button-secondary" @click="close" />
        <Button label="测试" icon="pi pi-play" class="p-button-primary" @click="testCapability" :disabled="!selectedCapability || testing" :loading="testing" />
      </div>
    </template>
  </Dialog>
</template>

<script>
import { ref, computed, watch } from 'vue';
import axios from 'axios';

export default {
  name: 'CapabilityTester',
  props: {
    visible: {
      type: Boolean,
      required: true
    },
    serviceId: {
      type: Number,
      required: true
    }
  },
  emits: ['update:visible'],
  setup(props, { emit }) {
    const loading = ref(false);
    const testing = ref(false);
    const capabilities = ref([]);
    const selectedCapability = ref(null);
    const parameters = ref({});
    const testResult = ref(null);
    
    // 计算属性：参数schema
    const parametersSchema = computed(() => {
      if (!selectedCapability.value || !selectedCapability.value.parameters) {
        return null;
      }
      try {
        return JSON.parse(selectedCapability.value.parameters);
      } catch (e) {
        console.error('解析参数schema失败:', e);
        return null;
      }
    });
    
    // 判断参数是否必填
    const isRequired = (key) => {
      return parametersSchema.value && 
             parametersSchema.value.required && 
             parametersSchema.value.required.includes(key);
    };
    
    // 监听选中的能力变化，重置参数
    watch(selectedCapability, () => {
      parameters.value = {};
      testResult.value = null;
    });
    
    // 加载服务能力
    const loadCapabilities = async () => {
      loading.value = true;
      try {
        const response = await axios.get(`/api/mcp_service_tester/capabilities/${props.serviceId}`);
        capabilities.value = response.data.capabilities;
      } catch (error) {
        console.error('加载服务能力失败:', error);
      } finally {
        loading.value = false;
      }
    };
    
    // 测试能力
    const testCapability = async () => {
      if (!selectedCapability.value) return;
      
      testing.value = true;
      testResult.value = null;
      
      try {
        const response = await axios.post(
          `/api/mcp_service_tester/test_capability/${props.serviceId}/${selectedCapability.value.id}`,
          selectedCapability.value.cap_type === 'tool' ? parameters.value : null
        );
        testResult.value = response.data;
      } catch (error) {
        console.error('测试能力失败:', error);
        testResult.value = {
          success: false,
          error: error.response?.data?.detail || '测试失败，请检查服务状态'
        };
      } finally {
        testing.value = false;
      }
    };
    
    // 关闭对话框
    const close = () => {
      emit('update:visible', false);
    };
    
    // 监听对话框可见性变化，加载能力列表
    watch(() => props.visible, (newVal) => {
      if (newVal) {
        loadCapabilities();
      } else {
        // 重置状态
        selectedCapability.value = null;
        parameters.value = {};
        testResult.value = null;
      }
    });
    
    return {
      loading,
      testing,
      capabilities,
      selectedCapability,
      parameters,
      parametersSchema,
      testResult,
      isRequired,
      testCapability,
      close
    };
  }
};
</script>

<style scoped>
.p-dialog-content {
  max-height: 70vh;
  overflow-y: auto;
}
</style>