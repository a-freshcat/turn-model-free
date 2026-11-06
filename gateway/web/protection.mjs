// Only local, allowlisted diagnostics are copied. Nothing is sent to AI automatically.
const host = document.getElementById('protection-panel')
let latest = null
const originalTitle = document.title
const message = document.createElement('span')
const pause = document.createElement('button')
const resume = document.createElement('button')
const copy = document.createElement('button')
const result = document.createElement('span')
message.className = 'protection-message'
result.className = 'ofm_note'
for (const button of [pause,resume,copy]) { button.type='button'; button.className='ofm_btn' }
pause.textContent='暂停模型调用'
resume.textContent='检查后恢复'
copy.textContent='复制诊断给 AI'
if (host) host.replaceChildren(message,pause,copy,resume,result)

async function request(body) {
  const response=await fetch('/ui/protection',{credentials:'same-origin',
    method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},
    body:body?JSON.stringify(body):undefined})
  if(response.status===401) { host.hidden=true; return null }
  const data=await response.json()
  if(!response.ok) throw new Error(data.error?.message || '保护状态读取失败')
  return data
}
function render(data) {
  if(!data)return
  latest=data
  host.hidden=false
  host.classList.toggle('protection-paused',data.paused)
  message.textContent=data.paused
    ? data.message+' 请复制诊断信息，发给 AI 排查后手动恢复。'+(data.persisted?'':' 暂停状态保存失败，请检查磁盘权限。')
    : data.recent_failure_count
      ? `最近一分钟有 ${data.recent_failure_count} 次上游故障；请复制诊断信息发给 AI 检查，连续故障会暂停调用。`
      : data.recent_network_failure_count
        ? `最近一分钟有 ${data.recent_network_failure_count} 次网络或临时服务故障，按短退避处理，不代表账号被限制。持续失败时可复制诊断给 AI。`
      : `调用保护已启用 · 最多 ${data.max_active_requests} 个对话并发 / ${data.max_dispatch_per_minute} 次模型调度每分钟`
  const occurred=data.paused?data.occurred_at:data.last_issue_at
  if(occurred)message.textContent+='（'+new Date(occurred*1000).toLocaleString()+'）'
  pause.hidden=data.paused
  resume.hidden=!data.paused
  copy.hidden=!data.paused&&!data.recent_failure_count&&!data.recent_network_failure_count
  document.title=data.paused?'[调用已暂停] '+originalTitle:originalTitle
}
async function update(){ try{render(await request())}catch{if(latest?.paused)result.textContent='状态刷新失败，暂停提醒保留。'} }
pause.addEventListener('click',async()=>{
  try{render(await request({method:'protection.pause',payload:{}}))}catch(error){result.textContent=error.message}
})
resume.addEventListener('click',async()=>{
  if(!window.confirm('确认已检查暂停原因，并了解恢复不会解除上游封禁或账号冷却？'))return
  try{render(await request({method:'protection.resume',payload:{acknowledge:true}}));result.textContent='已恢复本服务调用；账号自身限制仍然有效。'}catch(error){result.textContent=error.message}
})
copy.addEventListener('click',async()=>{
  if(!latest)return
  const diagnostic={component:'turn-model-free gateway',
    reason:latest.reason,http_status:latest.http_status,
    occurred_at:latest.occurred_at?new Date(latest.occurred_at*1000).toISOString():null,
    recent_failure_count:latest.recent_failure_count,last_issue_at:latest.last_issue_at,
    recent_network_failure_count:latest.recent_network_failure_count,
    paused:latest.paused,state_saved:latest.persisted,
    max_active_requests:latest.max_active_requests,max_dispatch_per_minute:latest.max_dispatch_per_minute}
  const text='请协助排查网关暂停原因；请勿要求上传账号配置、Cookie、访问令牌或刷新令牌。\n'+JSON.stringify(diagnostic,null,2)
  try{await navigator.clipboard.writeText(text);result.textContent='诊断已复制，请自行粘贴到 AI 对话。'}catch{
    result.textContent='剪贴板不可用，请选中以下诊断复制：'+text
  }
})
if(host){
  await update()
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)update()})
  setInterval(()=>{if(!document.hidden)update()},5000)
}
