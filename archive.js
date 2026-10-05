/* Archive controls share the existing library and detail panel. */
let archiveState={root:'',items:[],batches:[]},archiveBusy=false,archiveCancelled=false,archiveXHR=null,archiveFiles=new Map(),activeArchiveBatch='',resolveItem=null,archiveLoadSequence=0,queuedArchiveFiles=null;
const archiveFields=()=>Object.fromEntries(['year','category','kind','project'].map(k=>[k,$('#archive_'+k).value.trim()]));
function archiveRefreshEntry(){
  $('#archiveEntry').classList.toggle('hidden',view!=='home');
  $('#archiveRootLabel').textContent=archiveState.root?'档案目录：'+archiveState.root:'首次使用请设置档案目录';
  $('#entryRoot').textContent=archiveState.root||'设置档案目录后，即可拖入材料';
  $('#entryPreset').textContent='本次归档：'+(Object.values(archiveFields()).filter(Boolean).join(' / ')||'根据文件名自动识别');
  $('#archiveOpen').textContent=archiveBusy?'正在处理 · 查看进度':'本次归档信息与结果';
  $('#archiveCancel').disabled=!archiveBusy;
  $('#archivePick').disabled=archiveBusy;
}
async function loadArchive(batchId=activeArchiveBatch){let sequence=++archiveLoadSequence;let next=await api('archive-state'+(batchId?'?batch_id='+batchId:''));if(sequence!==archiveLoadSequence)return;archiveState=next;activeArchiveBatch=archiveState.batch_id;archiveRefreshEntry();renderArchiveItems()}
function showArchive(batchId=null){
  if(!archiveBusy){if(view==='files'||view==='organize'){if($('#year').value)$('#archive_year').value=$('#year').value;if($('#category').value)$('#archive_category').value=$('#category').value}}
  if(!$('#archiveDialog').open)$('#archiveDialog').showModal();safe(()=>loadArchive(typeof batchId==='string'?batchId:activeArchiveBatch));
}
function showArchiveRoot(){ $('#archiveRootInput').value=archiveState.root;$('#archiveRootDialog').showModal() }
function archiveInputsDisabled(disabled){document.querySelectorAll('.archive-presets input').forEach(e=>e.disabled=disabled);$('#archiveRootChange').disabled=disabled;$('#archiveHistory').disabled=disabled}
function renderArchiveItems(){
  let items=archiveState.items||[],counts=archiveState.counts||{};
  $('#archiveSummary').textContent=items.length?`本批 ${items.length} 个文件 · 已归档 ${counts.ready||0} · 已存在 / 关联 ${(counts.existing||0)+(counts.linked||0)} · 待确认 ${(counts.pending||0)+(counts.conflict||0)} · 失败 ${counts.failed||0} · 取消 ${counts.cancelled||0}`:'拖入后将自动复制到档案库，并建立分类与搜索记录。';
  $('#archiveQueue').innerHTML=items.map(item=>{
    let fields=item.fields||{},ok=['ready','existing','linked'].includes(item.status),pending=['pending','conflict'].includes(item.status);
    let evidence=Object.entries(item.sources||{}).map(([k,v])=>({year:'年度',category:'业务',kind:'材料',project:'活动',tags:'标签'}[k]||k)+'：'+v).join('；');
    let percent=item.size?Math.round(item.received/item.size*100):ok||pending?100:0;
    return `<div class="archive-item" data-archive-item="${item.id}"><div><strong>${esc(item.name)}</strong><div class="detail">${esc([fields.year,fields.category,fields.kind,fields.project].filter(Boolean).join(' / ')||'信息待识别')} · ${bytes(item.size)}</div>${item.path?`<div class="detail">${esc(item.path)}</div>`:''}${evidence?`<div class="detail">${esc(evidence)}</div>`:''}${item.status==='receiving'?`<div class="archive-progress"><div style="width:${percent}%"></div></div>`:''}${item.error?`<div class="detail error-status">${esc(item.error)}</div>`:''}<div class="actions">${pending?`<button data-resolve="${item.id}">${item.status==='conflict'?'处理重复位置':'补全归档信息'}</button>`:''}${['failed','cancelled'].includes(item.status)?`<button data-retry="${item.id}" ${archiveBusy?'disabled':''}>重试此文件</button>`:''}${item.pending_file?`<button data-pending-file="${item.file_id}">补全原归档记录</button>`:''}${item.file_id?`<button data-archive-folder="${item.file_id}">归档位置</button>`:''}</div></div><div class="item-status ${ok?'ok-status':pending?'pending-status':item.status==='failed'?'error-status':''}">${esc(item.label)}${item.status==='receiving'?'<div class="detail">'+percent+'%</div>':''}</div></div>`
  }).join('');
  $('#archiveQueue').querySelectorAll('[data-resolve]').forEach(b=>b.onclick=()=>showArchiveResolve(b.dataset.resolve));
  $('#archiveQueue').querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>safe(()=>retryArchive(b.dataset.retry)));
  $('#archiveQueue').querySelectorAll('[data-pending-file]').forEach(b=>b.onclick=()=>safe(async()=>{let item=await api('archive-find?file_id='+b.dataset.pendingFile);showArchiveResolve(item.id,item)}));
  $('#archiveQueue').querySelectorAll('[data-archive-folder]').forEach(b=>b.onclick=()=>safe(async()=>{let r=await api('folder',{ids:[+b.dataset.archiveFolder]});if(r.results[0]?.result!=='成功')toast(r.results[0]?.result)}));
  let history=$('#archiveHistory'),value=activeArchiveBatch;history.innerHTML='<option value="">最近批次</option><option value="pending">全部待确认事项</option>'+(archiveState.batches||[]).map(b=>`<option value="${b.id}">${esc(b.created_at)} · ${b.id.slice(-6)}</option>`).join('');history.value=value;
}
function uploadChunk(id,offset,blob){return new Promise((resolve,reject)=>{
  let xhr=new XMLHttpRequest();archiveXHR=xhr;xhr.open('POST',`/api/archive-chunk?id=${id}&offset=${offset}`);xhr.setRequestHeader('X-Token',TOKEN);xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.timeout=120000;
  xhr.upload.onprogress=e=>{if(!e.lengthComputable)return;let item=archiveState.items.find(i=>i.id===id);if(!item)return;let percent=item.size?Math.floor((offset+e.loaded)/item.size*100):100;let element=$(`[data-archive-item="${id}"]`);if(element){let bar=element.querySelector('.archive-progress div');if(bar)bar.style.width=percent+'%';let label=element.querySelector('.item-status .detail');if(label)label.textContent=percent+'%'}};
  xhr.onload=()=>{archiveXHR=null;try{let b=JSON.parse(xhr.responseText);if(xhr.status>=400)reject(Error(b.error||'接收失败'));else resolve(b)}catch(e){reject(Error('接收响应异常，请重试'))}};
  xhr.onerror=()=>{archiveXHR=null;reject(Error('接收连接中断，请重试'))};xhr.ontimeout=xhr.onerror;xhr.onabort=()=>{archiveXHR=null;reject(Error('已取消'))};xhr.send(blob);
})}
async function sendArchiveFile(item,file){
  let chunkSize=archiveState.chunk_size||4194304;
  for(let offset=0;offset<file.size;offset+=chunkSize){if(archiveCancelled)throw Error('已取消');await uploadChunk(item.id,offset,file.slice(offset,offset+chunkSize));item.received=Math.min(offset+chunkSize,file.size)}
  if(archiveCancelled)throw Error('已取消');let current=archiveState.items.find(i=>i.id===item.id);if(current){current.status='staged';current.label='核对并保存中';current.received=file.size;renderArchiveItems()}await api('archive-complete',{id:item.id});
}
async function receiveArchiveFiles(files,savedPresets=null){
  if(archiveBusy)return toast('请等待当前批次完成');
  if(!files.length)return;
  if(files.some(f=>!f.name))return toast('首版支持拖入文件，请打开文件夹后选择其中的文件');
  const presets=savedPresets||archiveFields();archiveBusy=true;archiveCancelled=false;archiveInputsDisabled(true);showArchive();archiveRefreshEntry();let began=false;
  try{
    await loadArchive();if(!archiveState.root){queuedArchiveFiles={files,presets};showArchiveRoot();toast('先设置档案目录，保存后会继续接收所选文件');return}
    let manifest=files.map(f=>({name:f.name,size:f.size,last_modified:f.lastModified}));
    let batch=await api('archive-begin',{files:manifest,presets});activeArchiveBatch=batch.batch_id;began=true;
    batch.items.forEach((item,index)=>archiveFiles.set(item.id,files[index]));await loadArchive();
    for(let index=0;index<batch.items.length;index++){
      if(archiveCancelled)break;let item=batch.items[index],file=files[index];archiveFiles.set(item.id,file);
      try{await sendArchiveFile(item,file)}catch(e){if(!archiveCancelled){await api('archive-fail',{id:item.id,error:e.message});toast(item.name+'：'+e.message)}}
      await loadArchive();
    }
  }finally{
    if(archiveCancelled&&began)await api('archive-cancel',{batch_id:activeArchiveBatch});
    archiveBusy=false;archiveXHR=null;archiveInputsDisabled(false);await loadArchive();await refresh();archiveRefreshEntry();
    if(!archiveCancelled&&began)toast('本批文件处理完成，可查看归档结果');
  }
}
async function retryArchive(id){
  let original=archiveFiles.get(id);
  archiveBusy=true;archiveCancelled=false;archiveInputsDisabled(true);archiveRefreshEntry();
  try{let retry=await api('archive-retry',{id,has_file:!!original});if(retry.needs_upload){toast('请重新拖入原文件；成功档案会自动核对重复。');return}if(!retry.completed){await loadArchive();await sendArchiveFile({id},original)}}
  catch(e){await api('archive-fail',{id,error:e.message});toast(e.message)}
  finally{archiveBusy=false;archiveInputsDisabled(false);await loadArchive();await refresh();archiveRefreshEntry()}
}
function showArchiveResolve(id,provided=null){
  resolveItem=provided||archiveState.items.find(i=>i.id===id);let f=resolveItem.fields||{};
  $('#resolveName').textContent=resolveItem.name;$('#resolveContext').textContent=resolveItem.error||resolveItem.reason||'';
  ['year','category','kind','project'].forEach(k=>$('#resolve_'+k).value=f[k]||'');
  $('#resolveLink').classList.toggle('hidden',resolveItem.status!=='conflict');
  $('#resolveCandidates').innerHTML=(resolveItem.candidates||[]).map(c=>`<option value="${c.id}">${esc([c.year,c.category,c.project,c.name].filter(Boolean).join(' / '))}</option>`).join('');
  $('#resolveCopy').textContent=resolveItem.status==='conflict'?'按本次用途另存':'确认并完成归档';
  $('#resolveCopy').disabled=false;$('#resolveLinkButton').disabled=false;$('#archiveResolveDialog').showModal();
}
async function resolveArchive(decision){
  $('#resolveCopy').disabled=true;$('#resolveLinkButton').disabled=true;
  try{let fields=Object.fromEntries(['year','category','kind','project'].map(k=>[k,$('#resolve_'+k).value.trim()]));await api('archive-resolve',{id:resolveItem.id,fields,decision,link_id:+$('#resolveCandidates').value});$('#archiveResolveDialog').close();await loadArchive();await refresh();toast('归档处理完成')}
  finally{$('#resolveCopy').disabled=false;$('#resolveLinkButton').disabled=false}
}
function bindDrop(element){
  element.addEventListener('dragover',e=>{e.preventDefault();e.dataTransfer.dropEffect='copy';element.classList.add('drag-active')});
  element.addEventListener('dragleave',e=>{if(!element.contains(e.relatedTarget))element.classList.remove('drag-active')});
  element.addEventListener('drop',e=>{e.preventDefault();element.classList.remove('drag-active');let items=[...e.dataTransfer.items];if(items.some(i=>i.webkitGetAsEntry?.()?.isDirectory))return toast('请打开文件夹，将里面的文件拖入。');safe(()=>receiveArchiveFiles([...e.dataTransfer.files]))});
}
$('#archiveOpen').onclick=showArchive;$('#archiveLibraryOpen').onclick=showArchive;$('#archiveEntryDrop').onclick=showArchive;$('#archivePendingOpen').onclick=()=>showArchive('pending');
$('#archivePick').onclick=()=>$('#archiveFileInput').click();$('#archiveDrop').onclick=()=>{if(!archiveBusy)$('#archiveFileInput').click()};
$('#archiveFileInput').onchange=e=>{let files=[...e.target.files];e.target.value='';safe(()=>receiveArchiveFiles(files))};
$('#archiveRootChange').onclick=showArchiveRoot;$('#archiveRootSave').onclick=()=>safe(async()=>{await api('archive-root',{path:$('#archiveRootInput').value});$('#archiveRootDialog').close();await loadArchive();toast('档案目录已保存');if(queuedArchiveFiles){let queued=queuedArchiveFiles;queuedArchiveFiles=null;await receiveArchiveFiles(queued.files,queued.presets)}});
$('#archiveRootDialog').addEventListener('cancel',()=>{queuedArchiveFiles=null});
document.querySelectorAll('.archive-presets input').forEach(input=>input.addEventListener('input',archiveRefreshEntry));
$('#editCompleteArchive').onclick=()=>safe(async()=>{let item=await api('archive-find?file_id='+editIds[0]);$('#editDialog').close();showArchiveResolve(item.id,item)});
$('#archiveCancel').onclick=()=>{archiveCancelled=true;if(archiveXHR)archiveXHR.abort();toast('正在取消未完成接收，已归档文件保留')};
$('#archiveHistory').onchange=e=>safe(()=>loadArchive(e.target.value));
$('#resolveCopy').onclick=()=>safe(()=>resolveArchive('copy'));$('#resolveLinkButton').onclick=()=>safe(()=>resolveArchive('link'));
$('#archiveOpenFiles').onclick=()=>{ $('#archiveDialog').close();navigate('files','archive-copies')};
bindDrop($('#archiveEntryDrop'));bindDrop($('#archiveDrop'));
$('#archiveEntryDrop').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showArchive()}};
$('#archiveDrop').onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&!archiveBusy){e.preventDefault();$('#archiveFileInput').click()}};
document.addEventListener('dragover',e=>{if([...e.dataTransfer.types].includes('Files'))e.preventDefault()});
document.addEventListener('drop',e=>{if([...e.dataTransfer.types].includes('Files'))e.preventDefault()});
window.addEventListener('beforeunload',e=>{if(archiveBusy){e.preventDefault();e.returnValue='文件正在接收'}});
safe(()=>loadArchive());

