/**
 * 主站后端请求实例（/api -> 后端 8000）
 * 供 智能问答、知识库 等模块使用
 */
import axios from 'axios'

export const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' }
})

export default request
