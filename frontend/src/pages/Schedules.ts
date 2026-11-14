import {defineComponent,ref,onMounted} from 'vue'
import {schedules,loadSchedules,scheduleApi} from '../stores/schedule'
import {reports,loadReports} from '../stores/report'
import {canApprove} from '../stores/auth'
import {useTask} from '../hooks/useTask'
import {usePolling} from '../hooks/usePolling'
import {timestamp} from '../utils/format'
import Notice from '../components/common/Notice'
import EmptyState from '../components/common/EmptyState'

export default defineComponent({components:{Notice,EmptyState},setup(){
 const state=useTask()
 const form=ref({name:'',source_report_id:null as number|null,daily_time:'08:30',enabled:true})
 onMounted(()=>state.task(async()=>{await Promise.all([loadSchedules(),loadReports()])}))
 usePolling(loadSchedules,15000)
 function create(){state.task(async()=>{await scheduleApi.create(form.value);await loadSchedules();state.notice.value='报送计划已保存，worker将在到期时生成站内快照';form.value.name=''})}
 function toggle(id:number,enabled:boolean){state.task(async()=>{await scheduleApi.toggle(id,enabled);await loadSchedules()})}
 function catchUp(id:number){state.task(async()=>{const result=await scheduleApi.catchUp(id);await loadSchedules();const count=result.delivered_ids.length;const progress=result.schedule.progress;state.notice.value=count?('已逐日补齐 '+count+' 个业务日快照，最新 '+progress.last_delivered_date+'。'):'没有新的逾期日期需要补报。';if(progress.blocked_date)state.error.value='补报停在该日 '+progress.blocked_date+'：'+(progress.blocked_reason||'没有可用纪要')})}
 function download(id:number,date:string){state.task(()=>scheduleApi.exportDelivery(id,date))}
 return {schedules,reports,canApprove,form,timestamp,...state,create,toggle,catchUp,download}
},template:`<section>
<div class="page-title"><p class="eyebrow">06 / DAILY DELIVERY</p><h1>报送计划</h1><p>每天固定时刻，把已确认的编排透视为一份站内快照。停机跨过的报送日会逐日补齐，时间均为北京时间。</p></div>
<Notice :error="error" :notice="notice"/>
<div class="split-layout"><div>
<article v-for="item in schedules" class="plan-row">
<header><div><p class="eyebrow">DELIVERY / 每日 {{item.daily_time}}</p><h2>{{item.name}}</h2></div><span class="tag">{{item.enabled?'运行中':'已暂停'}}</span></header>
<p>下次到期：{{item.enabled?item.progress.next_due_date:'暂停期间不运行'}}</p>
<p>上次报送：{{item.last_run?timestamp(item.last_run):'等待首次到期'}}</p>
<p>已落库：{{item.progress.delivered_count}} 个日期快照 · 最新业务日 {{item.progress.last_delivered_date||'—'}}</p>
<p v-if="item.progress.blocked_date" class="notice error">阻断日期 {{item.progress.blocked_date}}：{{item.progress.blocked_reason}}<span>该日不会被跳过，补报与定时任务都会先处理它。</span></p>
<p v-else-if="item.last_error" class="notice error">{{item.last_error}}</p>
<div class="action-row">
 <button v-if="canApprove" class="quiet" :disabled="busy" @click="toggle(item.id,!item.enabled)">{{item.enabled?'暂停报送':'启用报送'}}</button>
 <button v-if="canApprove" :disabled="busy" @click="catchUp(item.id)">立即补报逾期日期</button>
 <small>仅保存至本排演册；不会发送邮件或外部消息。</small>
</div>
<div v-for="delivery in item.deliveries.slice(0,7)" class="report-row">
 <div>
  <h3>{{delivery.business_date}} · 固定快照</h3>
  <p>{{delivery.row_count}} 个日期/厅记录 · 落库 {{timestamp(delivery.delivered_at)}}</p>
  <p v-if="delivery.cutoff_at">截止 {{timestamp(delivery.cutoff_at)}} · 来源：<span v-for="(source,index) in delivery.sources" :key="source.id"><span v-if="index">、</span>{{source.venue}}《{{source.title}}》（{{timestamp(source.known_at)}} 确认）</span></p>
  <p v-if="!delivery.sources.length" class="note-small">早期快照未记录逐日来源。</p>
 </div>
 <button class="quiet" :disabled="busy" @click="download(item.id,delivery.business_date)">下载透视 CSV ↓</button>
</div>
<p v-if="!item.deliveries.length" class="note-small">还没有快照。停机跨过多个报送日后点击“立即补报”，会从最早逾期日起逐日补齐。</p>
<p class="note-small">已生成快照不会因源纪要后续修改而覆盖，也不会重复写入。下载内容与该业务日快照一致。</p>
</article>
<EmptyState v-if="!schedules.length" title="尚无报送计划" text="请主管创建每日计划，分析员和值班组可以查看并下载已生成快照。"/>
</div>
<aside class="paper-note">
<template v-if="canApprove"><h3>新建每日计划</h3>
<form @submit.prevent="create">
<label>计划名称<input v-model="form.name" minlength="2" maxlength="100" required placeholder="如：每天开场前报送"></label>
<label>数据来源<select v-model="form.source_report_id"><option :value="null">各厅在截止时刻前最新确认的纪要</option><option v-for="report in reports" :value="report.id">{{report.title}}</option></select></label>
<label>报送时刻（北京时间）<input type="time" v-model="form.daily_time" required></label>
<label class="check"><input type="checkbox" v-model="form.enabled">保存后启用</label>
<button class="primary" :disabled="busy">保存计划</button>
</form></template>
<h3>随报附带 / 指标口径</h3>
<p>行：预演日期；列：场馆 × 指标。</p>
<p><b>预测人数</b>：该厅该日点预测，单位人。<br><b>预测上座率</b>：预测人数 ÷ 场馆容量 × 100%。<br><b>引导员建议</b>：向上取整（90%上界 ÷ 每名承载人数），至少2名。</p>
<p>每个报送业务日只取该日报送时刻前各厅最新确认的纪要，按日期与场馆聚合，后续确认的纪要不会回溯进旧快照。</p>
<p class="note-small">固定来源计划始终使用指定纪要；若该纪要晚于某日报送时刻才确认，该日会停住并说明原因，不会提前取用。停机后从最早逾期日逐日补齐，worker中断后可从已落库日期续跑；并发补报同一计划不会重复或覆盖已有快照。</p>
</aside></div>
</section>`})
