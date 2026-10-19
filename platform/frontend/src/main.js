import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import {
  ArrowDown, Box, CircleCheckFilled, CircleCloseFilled, Coin, Cpu, DataAnalysis,
  Delete, Document, EditPen, Files, Folder, FolderOpened, Grid, Histogram, List,
  Loading, Monitor, Plus, Search, Setting, Timer, User,
} from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import { loadObsConfig } from './api/obsConfig'

// 运行时加载 OBS 桶配置（失败自动回退，不阻塞启动）
if (localStorage.getItem('token')) {
  loadObsConfig()
}

const app = createApp(App)
app.use(createPinia())
app.use(router)

// Element Plus 组件由 vite 的 unplugin-vue-components 按需引入（见 vite.config.js）。
// 这里原先又 app.use(ElementPlus) 全量注册了一遍，打包体积翻倍；只补两样按需插件
// 覆盖不到的东西：
//   1. v-loading 指令（模板里 25 处使用，指令不会自动按需注册）
//   2. 图标组件（模板里以 <Cpu /> 这类标签直接使用）
app.use(ElLoading)

const icons = {
  ArrowDown, Box, CircleCheckFilled, CircleCloseFilled, Coin, Cpu, DataAnalysis,
  Delete, Document, EditPen, Files, Folder, FolderOpened, Grid, Histogram, List,
  Loading, Monitor, Plus, Search, Setting, Timer, User,
}
for (const [key, component] of Object.entries(icons)) {
  app.component(key, component)
}

app.mount('#app')
