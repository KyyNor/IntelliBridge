import { createApp } from 'vue'
import App from './App.vue'
import PrimeVue from 'primevue/config'
import { definePreset } from '@primeuix/themes';
import Lara from '@primeuix/themes/lara';
import 'primeicons/primeicons.css'
import 'primeicons/fonts/primeicons.woff2'
import 'primeicons/fonts/primeicons.woff'

// PrimeVue组件
import Dialog from 'primevue/dialog'
import TabView from 'primevue/tabview'
import TabPanel from 'primevue/tabpanel'
import Accordion from 'primevue/accordion'
import AccordionTab from 'primevue/accordiontab'
import ProgressSpinner from 'primevue/progressspinner'

const MyTheme = definePreset(Lara, {
    semantic: {
        primary: {
            50: '{sky.50}',
            100: '{sky.100}',
            200: '{sky.200}',
            300: '{sky.300}',
            400: '{sky.400}',
            500: '{sky.500}',
            600: '{sky.600}',
            700: '{sky.700}',
            800: '{sky.800}',
            900: '{sky.900}',
            950: '{sky.950}'
        }
    }
});

const app = createApp(App)
app.use(PrimeVue, {
    theme: {
        preset: MyTheme,
        options: {
            prefix: 'p',
            darkModeSelector: 'system',
            cssLayer: false
        }
    }
})
app.mount('#app')