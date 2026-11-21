import axios from 'axios'

export interface OfflineSource {
  id:string
  name:string
  status:string
  product?:string
  product_label?:string
  version?:string
  ddl?:string
  analysis?:string
  error?:string
  object_count?:number
  relation_count?:number
}

export interface OfflineProduct {
  id:string
  label:string
  modes:{id:string;label:string}[]
}

export const offlineApi = axios.create({baseURL:'/offline-modeling/v1',timeout:40_000})

export function offlineErrorMessage(error:unknown, fallback:string) {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 401) return '请使用离线建模访问凭据登录后重试'
    if (typeof error.response?.data?.detail === 'string') return error.response.data.detail
  }
  return fallback
}

export function offlineImportPath(id:string) {
  return `/imports/${encodeURIComponent(id)}`
}
