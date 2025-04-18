<template>
  <Dialog :visible="dialogVisible" modal header="测试MCP能力" :style="{ width: '60vw' }" class="p-fluid" :closeOnEscape="true" :dismissableMask="true" @update:visible="updateVisible">
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
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import ProgressSpinner from 'primevue/progressspinner'
import Checkbox from 'primevue/checkbox'
import Textarea from 'primevue/textarea'

export default {
  components: {
    Button, Dialog, ProgressSpinner, Checkbox, Textarea,
    Select, InputText, InputNumber
  },
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
    const dialogVisible = ref(false);
    
    // 监听props.visible变化，同步到内部状态
    watch(() => props.visible, (newVal) => {
      dialogVisible.value = newVal;
      if (newVal) {
        loadCapabilities();
      } else {
        // 重置状态
        selectedCapability.value = null;
        parameters.value = {};
        testResult.value = null;
      }
    }, { immediate: true });
    
    // 更新visible状态并触发事件
    const updateVisible = (newVal) => {
      dialogVisible.value = newVal;
      emit('update:visible', newVal);
    };
    
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
    
    // 删除原有的watch函数，因为已经在新的watch中处理了对话框可见性变化和加载能力列表的逻辑，并在return语句中添加新的dialogVisible和updateVisible变量
    
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
      close,
      dialogVisible,
      updateVisible
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