import {request,download} from './client'
import type {DeliverySchedule} from '../types/domain'
export const list=()=>request<DeliverySchedule[]>('/schedules')
export const create=(body:unknown)=>request<DeliverySchedule>('/schedules',body)
export const toggle=(id:number,enabled:boolean)=>request<DeliverySchedule>('/schedules/'+id+'/toggle',{enabled})
export const catchUp=(id:number)=>request<{schedule:DeliverySchedule;delivered_ids:number[]}>('/schedules/'+id+'/catch-up',{})
export const exportDelivery=(id:number,date:string)=>download('/schedules/'+id+'/deliveries/'+date+'/download','delivery-'+id+'-'+date+'.csv')
