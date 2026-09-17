'use strict';
const $ = id => document.getElementById(id);
let activitySignature='', state, token='', selected=new Set(), recordScope='all', focusId='', previewOffset=0, previewFile=0, activePlan, previewSequence=0, deleting=false;
let grouped=false,diagnosticPage=0;
let databaseBusy=false;
const deletedIds=new Set();
const diagnosticLabels={missing:'文件缺失',unreadable:'无法读取',unsafe:'路径越界',orphan:'归属不明'};
const collapsedProjects=new Set();
let navGroups=[],activeProject='',groupHeaders=new Map(),navButtons=new Map(),scrollFrame=0;
try{grouped=localStorage.getItem('cleaner.groupByProject')==='true';}catch{}
function projectKey(r){
  let path=(r.cwd||'').replaceAll('\\','/');
  if(path.toLowerCase().startsWith('//?/unc/'))path='//'+path.slice(8);
  else if(path.startsWith('//?/'))path=path.slice(4);
  return r.project_id?'project:'+r.project_id:'cwd:'+path.replace(/\/+$/,'').toLowerCase();
}
const fmt = n => {n=Number(n)||0; let u=0; while(n>=1024&&u<4){n/=1024;u++;}return `${n.toFixed(u?2:0)} ${['B','KiB','MiB','GiB','TiB'][u]}`;};
function element(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)uiText(e,text);if(cls)e.className=cls;return e;}
function content(tag,text,cls){const e=element(tag,undefined,cls);rawText(e,text);return e;}
function error(e){uiText($('error'),e.message||String(e));$('error').hidden=false;}
async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw new Error(v.error||r.statusText);return v;}
let sizeState=null,sizeRows=new Map(),sizeEdges={},sizeCache=new Map();
function scopeSize(ids){
 if(sizeState!==state){sizeState=state;sizeRows=new Map((state?.rows||[]).map(r=>[r.id,r]));sizeEdges=state?.spawn_edges||{};sizeCache=new Map();}
 const seen=new Set(),pending=[...ids];let size=0,count=0;
 while(pending.length){const id=pending.pop();if(seen.has(id))continue;seen.add(id);pending.push(...(sizeEdges[id]||[]));const row=sizeRows.get(id);if(row){size+=row.size||0;count++;}}
 return {size,count};
}
function rowSize(row){if(sizeState!==state)scopeSize([]);if(!sizeCache.has(row.id))sizeCache.set(row.id,scopeSize([row.id]));return sizeCache.get(row.id);}
function filtered(){const q=$('search').value.toLocaleLowerCase();return (state?.rows||[]).filter(r=>(!$('fileStatus').value||((r.missing_files||[]).length>0&&($('fileStatus').value!=='allMissing'||r.file_count===0)))&&(!$('project').value||projectKey(r)===$('project').value)&&[r.title,r.project,r.cwd,r.id].some(v=>(v||'').toLocaleLowerCase().includes(q))).sort((a,b)=>$('sort').value==='size'?rowSize(b).size-rowSize(a).size:(b.updated_at||0)-(a.updated_at||0));}
function selectionTotals(){
 const byId=new Map((state?.rows||[]).map(r=>[r.id,r]));
 const edges=new Map(Object.entries(state?.spawn_edges||{}));
 if(!state?.spawn_edges)for(const row of byId.values())for(const parent of row.parents||[]){if(!edges.has(parent.id))edges.set(parent.id,[]);edges.get(parent.id).push(row.id);}
 const seen=new Set(),pending=[...selected];let own=0,related=0,count=0,relatedCount=0;
 while(pending.length){const id=pending.pop();if(seen.has(id))continue;seen.add(id);pending.push(...(edges.get(id)||[]));const row=byId.get(id);if(!row)continue;count++;if(selected.has(id))own+=row.reclaimable;else{related+=row.reclaimable;relatedCount++;}}
 return {own,related,count,relatedCount};
}
function sizeSummary(own,related,relatedCount,count){return relatedCount?msg('selection_with_children',{own:fmt(own),children:fmt(related),count:relatedCount,total:count,size:fmt(own+related)}):msg('selection_size',{count,size:fmt(own)});}
function selection(){const totals=selectionTotals();uiText($('selection'),totals.count?sizeSummary(totals.own,totals.related,totals.relatedCount,totals.count):'未选择会话');$('plan').disabled=!totals.count||!state?.complete||deleting||databaseBusy;const visible=filtered();$('selectAll').checked=visible.length>0&&visible.every(r=>selected.has(r.id));$('selectAll').indeterminate=visible.some(r=>selected.has(r.id))&&!$('selectAll').checked;}

function problemCell(r){
  const cell=element('td',undefined,'problem-cell');const notes=new Map();
  const add=(label,detail)=>{if(!notes.has(label))notes.set(label,[]);notes.get(label).push(detail);};
  if(r.is_current||r.state?.includes('当前会话'))add('当前会话','当前会话受保护，不能删除。');
  else if(r.activity?.code==='busy')add('占用中',r.activity.reason);
  else if(r.activity?.code==='unknown')add('状态未确认',r.activity.reason);
  if(r.kind==='unknown')add('来源未知','无法确认该记录的来源，暂不允许删除。');
  for(const issue of r.issues||[]){
    let label='文件异常';
    if(issue.startsWith('文件缺失'))label='文件缺失';
    else if(issue.startsWith('文件读取失败'))label='无法读取';
    else if(issue.includes('路径超出'))label='路径越界';
    else if(issue.startsWith('归属冲突'))label='归属冲突';
    else if(issue.startsWith('硬链接文件'))label='硬链接';
    else if(issue.includes('不可读取或不存在'))label='文件缺失或不可读';
    else if(issue.includes('共享 / 冲突'))label='文件归属异常';
    add(label,issue);
  }
  for(const [label,details] of notes){
    const button=element('button',label,'problem-tag');I18N.attribute(button,'title',details.join('\n'));
    button.onclick=()=>{rawText($('problemTitle'),r.title);$('problemDetails').replaceChildren();
      for(const [name,texts] of notes){$('problemDetails').append(element('h3',name));for(const text of texts)$('problemDetails').append(element('p',text,'problem-detail'));}
      $('problemDialog').showModal();};
    cell.append(button);
  }
  return cell;
}
function selectAttached(id,checked){
 const edges=state?.display_edges||state?.spawn_edges||{},known=new Set((state?.rows||[]).map(r=>r.id)),seen=new Set(),pending=[id];
 while(pending.length){const current=pending.pop();if(seen.has(current))continue;seen.add(current);if(known.has(current))checked?selected.add(current):selected.delete(current);pending.push(...(edges[current]||[]));}
}
function sessionRow(r,child=false,last=false){
  const tr=element('tr',undefined,selected.has(r.id)?'session-row selected':'session-row');
  if(child)tr.classList.add('project-child');if(last)tr.classList.add('last-child');
  const check=element('input');check.type='checkbox';check.checked=selected.has(r.id);
  I18N.attribute(check,'aria-label',msg('select_session',{title:r.title}));
  check.onchange=()=>{selectAttached(r.id,check.checked);render();};
  const td=element('td');if(!child)td.append(check);tr.append(td);
  const title=element('td');title.className='session-title';const b=content('button',r.title,'title');b.title=r.title;b.onclick=()=>loadPreview(r.id);
  if(child){const content=element('div',undefined,'child-content');content.append(check,b);title.append(content);}
  else title.append(b,content('small',r.project||r.cwd,'subtitle'));
  if(recordScope==='all')tr.classList.add('categorized-record');
  title.title=r.cwd+'\n'+r.id;
  const total=rowSize(r),bytes=element('td',fmt(total.size),'bytes');
  if(total.count>1){bytes.append(element('small',msg('size_own_children',{size:fmt(r.size),count:total.count-1}),'size-breakdown'));I18N.attribute(bytes,'title',msg('size_tooltip',{total:fmt(total.size),own:fmt(r.size),children:fmt(total.size-r.size)}));}
  tr.append(title,bytes,element('td',r.updated_at?new Date(r.updated_at*1000).toLocaleDateString(I18N.language==='en'?'en-US':'zh-CN'):'未知'),problemCell(r));
  return tr;
}
function projectGroups(rows){
  const map=new Map();
  for(const row of rows){
    const key=projectKey(row);
    if(!map.has(key))map.set(key,{key,name:row.project||'未分配项目',paths:new Set(),rows:[],size:0,updated_at:0});
    const g=map.get(key);g.rows.push(row);g.size+=row.size;g.updated_at=Math.max(g.updated_at,row.updated_at||0);if(row.cwd)g.paths.add(row.cwd);
  }
  for(const group of map.values())group.size=scopeSize(group.rows.map(r=>r.id)).size;
  const sort=$('sort').value;
  return [...map.values()].sort((a,b)=>b[sort]-a[sort]||a.name.localeCompare(b.name,'zh-CN'));
}
function setActiveProject(key){
  activeProject=key;
  for(const [id,button] of navButtons){
    button.classList.toggle('active',id===key);
    if(id===key)button.setAttribute('aria-current','location');else button.removeAttribute('aria-current');
  }
}
function syncProjectPosition(){
  if(!grouped||!groupHeaders.size)return;
  const wrapper=document.querySelector('.tablewrap');
  const line=wrapper.getBoundingClientRect().top+wrapper.querySelector('thead').offsetHeight+6;
  let key=groupHeaders.keys().next().value;
  for(const [id,header] of groupHeaders){if(header.getBoundingClientRect().top<=line)key=id;else break;}
  if(key!==activeProject)setActiveProject(key);
}
function clearPreview(){
  ++previewSequence;focusId='';previewOffset=0;previewFile=0;
  uiText($('previewTitle'),msg('select_a_session'));$('previewId').hidden=true;uiText($('previewId'),'');
  uiText($('previewMeta'),msg('click_a_session_title_to_view_its_contents'));
  $('messages').replaceChildren();$('files').replaceChildren();
  $('more').hidden=true;$('more').disabled=false;uiText($('more'),msg('load_more'));
  $('files').closest('details').open=false;
  document.querySelector('.workspace>aside').scrollTop=0;
  $('clearPreview').disabled=true;
}
$('clearPreview').onclick=clearPreview;
function jumpToProject(group){
  if(collapsedProjects.delete(group.key))render();
  const header=groupHeaders.get(group.key);if(!header)return;
  const wrapper=document.querySelector('.tablewrap');
  wrapper.scrollTop+=header.getBoundingClientRect().top-wrapper.getBoundingClientRect().top-wrapper.querySelector('thead').offsetHeight;
  setActiveProject(group.key);
  header.querySelector('.group-toggle').focus({preventScroll:true});
  // Project navigation must never pick a session on the user's behalf.
  clearPreview();
}
function renderProjectNav(){
  const nav=$('projectNav');nav.hidden=!grouped;
  document.querySelector('.workspace').classList.toggle('with-project-nav',grouped);
  const list=$('projectNavItems');list.replaceChildren();navButtons=new Map();
  if(!grouped)return;
  const q=$('navSearch').value.trim().toLocaleLowerCase();
  const groups=navGroups.filter(g=>[g.name,...g.paths].some(s=>s.toLocaleLowerCase().includes(q)));
  uiText($('navCount'),q?`${groups.length} / ${navGroups.length}`:String(navGroups.length));
  for(const g of groups){
    const button=element('button',undefined,'nav-project');button.dataset.projectKey=g.key;
    button.title=[...g.paths].join('\n');
    const name=element('span',undefined,'nav-project-name');
    const folder=element('span',undefined,'folder-icon');folder.setAttribute('aria-hidden','true');
    name.append(folder,content('span',g.name));
    const meta=element('span',undefined,'nav-project-meta');meta.append(element('span',msg('session_count',{count:g.rows.length})),element('span',fmt(g.size)));
    button.append(name,meta);button.onclick=()=>jumpToProject(g);list.append(button);navButtons.set(g.key,button);
  }
  if(!groups.length)list.append(element('p',msg('no_matching_projects'),'nav-empty'));
  setActiveProject(activeProject);
}
const expandedThreads=new Set();
function appendRecordSections(body,rows,projectChild=false){
 const byId=new Map((state?.rows||[]).map(r=>[r.id,r])),matches=new Set(rows.map(r=>r.id)),visible=new Set(matches),parents=new Map();
 for(const [parent,children] of Object.entries(state?.display_edges||state?.spawn_edges||{}))for(const id of children){if(parent===id||!byId.has(parent)||!byId.has(id))continue;if(!parents.has(id))parents.set(id,new Set());parents.get(id).add(parent);}
 const parentOf=id=>{const first=parents.get(id)?.size===1?[...parents.get(id)][0]:null;const seen=new Set([id]);let current=first;while(current){if(seen.has(current))return null;seen.add(current);current=parents.get(current)?.size===1?[...parents.get(current)][0]:null;}return first;};
 for(const row of rows){let id=row.id;const seen=new Set([id]);while(parentOf(id)){id=parentOf(id);if(seen.has(id))break;seen.add(id);visible.add(id);}}
 const children=new Map();for(const id of visible){const parent=parentOf(id);if(parent&&visible.has(parent)){if(!children.has(parent))children.set(parent,[]);children.get(parent).push(id);}}
 const compare=(a,b)=>$('sort').value==='size'?rowSize(byId.get(b)).size-rowSize(byId.get(a)).size:(byId.get(b).updated_at||0)-(byId.get(a).updated_at||0);
 const emitted=new Set(),filtering=!!($('search').value||$('fileStatus').value);
 function emit(id,depth){if(emitted.has(id))return;emitted.add(id);const row=byId.get(id),items=(children.get(id)||[]).sort(compare),context=!matches.has(id),tr=sessionRow(row,false);tr.classList.add('hierarchy-row');tr.dataset.recordId=id;tr.style.setProperty('--record-depth',Math.min(depth,12));
  const cell=tr.querySelector('.session-title'),title=cell.querySelector('.title'),line=element('div',undefined,'tree-title');title.before(line);line.append(title);
  if(depth>0){
   cell.querySelector('.subtitle')?.remove();tr.classList.add('attached-row');
   if(row.record_type==='操作安全审批')uiText(title,msg('approval_child',{id:id.slice(-8)}));
   else if(row.title_missing)uiText(title,msg('agent_child',{id:id.slice(-8)}));
  }
  if(items.length){const toggle=element('button',filtering||expandedThreads.has(id)?'▾':'▸','tree-toggle');toggle.setAttribute('aria-expanded',String(filtering||expandedThreads.has(id)));I18N.attribute(toggle,'aria-label',msg('toggle_attached'));toggle.onclick=()=>{expandedThreads.has(id)?expandedThreads.delete(id):expandedThreads.add(id);render();};line.prepend(toggle);line.append(element('small',msg('attached_count',{count:items.length}),'attached-count'));}else line.prepend(element('span',undefined,'tree-spacer'));
  if(context){tr.classList.add('context-row');tr.querySelector('input').disabled=true;tr.querySelector('input').checked=false;cell.append(element('small',msg('parent_context'),'tree-meta'));}
  else if(depth===0&&row.kind!=='main')cell.append(element('small',row.record_type||msg('section_other'),'tree-meta'));
  body.append(tr);if(filtering||expandedThreads.has(id))for(const child of items)emit(child,depth+1);
 }
 const roots=[...visible].filter(id=>!visible.has(parentOf(id))).sort(compare);
 for(const id of roots.filter(id=>byId.get(id).kind==='main'))emit(id,0);
 const unlinked=roots.filter(id=>byId.get(id).kind!=='main');
 if(unlinked.length){const tr=element('tr',undefined,'record-section');
 const groupIds=new Set(),pending=[...unlinked],visited=new Set();while(pending.length){const id=pending.pop();if(visited.has(id))continue;visited.add(id);if(matches.has(id))groupIds.add(id);pending.push(...(children.get(id)||[]));}
 const check=element('input');check.type='checkbox';check.className='unlinked-select';const chosen=[...groupIds].filter(id=>selected.has(id)).length;check.checked=groupIds.size>0&&chosen===groupIds.size;check.indeterminate=chosen>0&&chosen<groupIds.size;check.disabled=!groupIds.size;I18N.attribute(check,'aria-label',msg('select_unlinked'));check.onchange=()=>{for(const id of groupIds)check.checked?selected.add(id):selected.delete(id);render();};const selectCell=element('td');selectCell.append(check);tr.append(selectCell);const cell=element('td');cell.colSpan=4;cell.append(element('strong',msg('unlinked_records')),element('span',msg('unlinked_note'),'section-count'));tr.append(cell);body.append(tr);for(const id of unlinked)emit(id,0);}
 // Malformed cycles must remain inspectable rather than disappearing.
 for(const id of visible)if(!emitted.has(id)&&!parentOf(id))emit(id,0);
}
function render(){
  const wrapper=document.querySelector('.tablewrap');const scrollTop=wrapper.scrollTop;
  const rows=filtered();const body=$('rows');body.replaceChildren();groupHeaders=new Map();
  navGroups=grouped?projectGroups(rows):[];
  $('groupByProject').classList.toggle('active',grouped);$('groupByProject').setAttribute('aria-pressed',String(grouped));
  if(!grouped){appendRecordSections(body,rows);}
  else for(const g of navGroups){
    const tr=element('tr',undefined,'project-group');tr.dataset.projectKey=g.key;groupHeaders.set(g.key,tr);
    const check=element('input');check.type='checkbox';check.className='group-select';
    const chosen=g.rows.filter(r=>selected.has(r.id)).length;check.checked=chosen===g.rows.length;check.indeterminate=chosen>0&&chosen<g.rows.length;
    I18N.attribute(check,'aria-label',msg('select_project',{title:g.name}));
    check.onchange=()=>{for(const r of g.rows)check.checked?selected.add(r.id):selected.delete(r.id);render();};
    const td=element('td');td.append(check);tr.append(td);
    const heading=element('td');heading.colSpan=4;
    const closed=collapsedProjects.has(g.key);
    const toggle=element('button',undefined,'group-toggle');toggle.setAttribute('aria-expanded',String(!closed));
    const arrow=element('span',undefined,'group-chevron');arrow.setAttribute('aria-hidden','true');
    const folder=element('span',undefined,'folder-icon');folder.setAttribute('aria-hidden','true');
    toggle.append(arrow,folder,content('span',g.name,'group-name'));
    toggle.onclick=()=>{closed?collapsedProjects.delete(g.key):collapsedProjects.add(g.key);render();};
    const stats=element('span',msg('group_size',{count:g.rows.length,size:fmt(g.size)}),'group-stats');
    const paths=[...g.paths].join(' · ');toggle.title=paths||'未记录工作目录';
    const groupBar=element('div',undefined,'group-heading');groupBar.append(toggle,stats);heading.append(groupBar);tr.append(heading);body.append(tr);
    if(!closed)appendRecordSections(body,g.rows,true);
  }
  uiText($('countLabel'),msg('list_all_records'));uiText($('count'),rows.length.toLocaleString());
  $('empty').hidden=rows.length>0||!state?.complete||!!state?.error;updateLoading();selection();renderProjectNav();wrapper.scrollTop=scrollTop;syncProjectPosition();
}
function locateDiagnosticThread(row){
  $('diagnosticsDialog').close();

  $('fileStatus').value='';
  $('project').value='';$('search').value=row.id;$('navSearch').value='';
  collapsedProjects.delete(projectKey(row));render();
  document.querySelector('.tablewrap').scrollTop=0;
  document.querySelector('.session-row .title')?.focus({preventScroll:true});
  loadPreview(row.id);
}
function renderDiagnostics(){
  if(!$('diagnosticsDialog').open)return;
  const records=state?.diagnostics||[];const byId=new Map((state?.rows||[]).map(r=>[r.id,r]));
  const q=$('diagnosticSearch').value.trim().toLocaleLowerCase();const kind=$('diagnosticKind').value;
  const matches=records.filter(d=>(!kind||d.kind===kind)&&[d.path,...(d.thread_ids||[]).map(id=>byId.get(id)?.title||id)].some(t=>t.toLocaleLowerCase().includes(q)));
  const pageCount=Math.max(1,Math.ceil(matches.length/25));diagnosticPage=Math.min(diagnosticPage,pageCount-1);
  const total=state?.diagnostic_count||0;
  uiText($('diagnosticSummary'),`共 ${total} 条扫描记录${total>records.length?`，当前提供前 ${records.length} 条明细`:''}。文件缺失不代表占用空间，可定位到会话清理残留条目；归属不明的文件不直接删除。`);
  const list=$('diagnosticRows');list.replaceChildren();
  for(const d of matches.slice(diagnosticPage*25,(diagnosticPage+1)*25)){
    const item=element('div',undefined,'diagnostic-record');
    const heading=element('div',undefined,'diagnostic-record-heading');heading.append(element('strong',diagnosticLabels[d.kind]||'扫描异常'),element('span',d.kind==='missing'?'文件已不存在':fmt(d.size)));
    item.append(heading,content('p',d.path,'diagnostic-path'));
    const linked=(d.thread_ids||[]).map(id=>byId.get(id)).filter(Boolean);
    for(const row of linked){const locate=element('button',msg('locate_session',{title:row.title}),'diagnostic-locate');locate.onclick=()=>locateDiagnosticThread(row);item.append(locate);}
    if(!linked.length)item.append(element('small',msg('no_confirmed_session_link_diagnostic_information_only')));
    const detail=element('details');detail.append(element('summary',msg('technical_details')),element('p',d.reason,'diagnostic-path'));item.append(detail);list.append(item);
  }
  if(!matches.length)list.append(element('p',msg('no_matching_diagnostics'),'nav-empty'));
  uiText($('diagnosticPage'),msg('diagnostic_page',{page:diagnosticPage+1,pages:pageCount,count:matches.length}));
  $('diagnosticPrev').disabled=diagnosticPage===0;$('diagnosticNext').disabled=diagnosticPage+1===pageCount;
}
function updateLoading(failure=''){
 const busy=!state?.complete&&!state?.error&&!failure;
 const hasRows=!!document.querySelector('#rows .session-row');
 const problem=failure||state?.error||'';
 $('listLoading').hidden=hasRows||(!busy&&!problem);
 $('listLoading').classList.toggle('load-failed',!!problem);
 uiText($('loadingTitle'),problem?'加载失败':(state?'正在扫描会话…':'正在加载会话…'));
 uiText($('loadingDetail'),problem?(problem+'；正在自动重试连接。'):(state?.status||'正在连接本地服务'));
 $('scanProgress').hidden=!busy||!hasRows;
 uiText($('scanProgress'),busy?(state?.status||'正在加载会话…'):'');
 document.querySelector('.tablewrap').setAttribute('aria-busy',String(busy));
}
async function poll(){try{const next=await api('/api/state');next.rows=next.rows.filter(r=>!deletedIds.has(r.id));token=next.csrf;const oldGen=state?.generation;const oldComplete=state?.complete;state=next;const ids=new Set(state.rows.map(r=>r.id));selected=new Set([...selected].filter(id=>ids.has(id)));uiText($('size'),fmt(state.rows.reduce((s,r)=>s+r.size,0)));uiText($('scanState'),state.status);I18N.attribute($('scanState'),'title',state.source);uiText($('cliState'),state.cli.available?'CLI 可用':'CLI 只读 / 检测中');I18N.attribute($('cliState'),'title',(state.cli.version||'')+'\n'+state.cli.message);if(state.error)error(state.error);const nextActivity=JSON.stringify(state.rows.map(r=>[r.state,r.issues]));if(oldGen!==state.generation||oldComplete!==state.complete||nextActivity!==activitySignature){activitySignature=nextActivity;const old=$('project').value;$('project').replaceChildren(element('option',msg('all_projects')));$('project').firstChild.value='';const groups=projectGroups(state.rows);for(const g of groups.sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'))){const duplicate=groups.some(other=>other.key!==g.key&&other.name===g.name);const o=content('option',g.name+(duplicate?' · '+([...g.paths][0]||g.key):''));o.value=g.key;$('project').append(o);}if([...$('project').options].some(o=>o.value===old))$('project').value=old;render();}else if(!state.complete)render();uiText($('openDiagnostics'),msg('diagnostic_count',{count:state.diagnostic_count}));renderDiagnostics();updateLoading();}catch(e){error(e);updateLoading(e.message);}finally{setTimeout(poll,state?.complete?6000:1500);}}
async function loadPreview(id,append=false){const sequence=++previewSequence;focusId=id;$('previewId').hidden=false;uiText($('previewId'),'ID：'+id);$('clearPreview').disabled=false;if(!append){previewOffset=0;previewFile=0;$('messages').replaceChildren();rawText($('previewTitle'),state.rows.find(r=>r.id===id)?.title||id);} if(!append){const row=state.rows.find(r=>r.id===id);if(row?.kind==='internal'){const info=element('div',undefined,'record-info');info.append(element('b',row.record_type||'内部记录'),element('p',row.record_type==='操作安全审批'?'Codex 为工具操作生成的安全审批，下面展示审批输入和结果。':'Codex 派生的代理任务，下面展示任务输入和回复。'));for(const parent of row.parents||[]){const link=element('button',msg('parent_session',{title:parent.title}),'diagnostic-locate');link.onclick=()=>locateDiagnosticThread(state.rows.find(r=>r.id===parent.id));info.append(link);}info.append(element('p',msg('record_id',{id:row.id})),element('p',msg('source_value',{source:row.source})));$('messages').append(info);}}uiText($('previewMeta'),msg('reading_preview'));$('more').disabled=true;uiText($('more'),msg('loading'));try{while(true){const previousOffset=previewOffset,previousFile=previewFile;const p=await api(`/api/preview?id=${encodeURIComponent(id)}&offset=${previewOffset}&file=${previewFile}`);if(sequence!==previewSequence)return;for(const m of p.messages){const box=element('div',undefined,'message');box.append(element('b',m.label),content('pre',m.text));$('messages').append(box);}uiText($('previewMeta'),p.note);if(!p.messages.length&&!append&&p.next===null)$('messages').append(element('p',msg('no_displayable_messages')));previewOffset=p.next;previewFile=p.file_index||0;$('more').hidden=p.next===null;$('files').replaceChildren(...p.files.map(f=>element('div',msg(f.shared?'shared_file_detail':'file_detail',{path:f.path,size:fmt(f.size),evidence:f.evidence}),'file')));
if(p.messages.length||p.next===null)break;
if(previousOffset===previewOffset&&previousFile===previewFile)throw new Error('读取位置没有前进，请重试。');
uiText($('previewMeta'),msg('looking_for_more_messages'));
$('more').hidden=false;
}
uiText($('more'),msg('load_more'));
}catch(e){if(sequence!==previewSequence)return;error(e);uiText($('previewMeta'),'预览失败：'+e.message);$('more').hidden=false;uiText($('more'),msg('retry_loading'));}finally{if(sequence===previewSequence)$('more').disabled=false;}}
for(const id of ['search','project','sort','fileStatus'])$(id).addEventListener(id==='search'?'input':'change',render);
$('navSearch').oninput=renderProjectNav;
document.querySelector('.tablewrap').addEventListener('scroll',()=>{if(scrollFrame)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=0;syncProjectPosition();});},{passive:true});
$('groupByProject').onclick=()=>{grouped=!grouped;try{localStorage.setItem('cleaner.groupByProject',String(grouped));}catch{}render();};
$('selectAll').onchange=()=>{for(const r of filtered())$('selectAll').checked?selected.add(r.id):selected.delete(r.id);render();};
$('more').onclick=()=>loadPreview(focusId,true);
$('settings').onclick=()=>{if(!state)return;$('settingsError').hidden=true;$('homeInput').value=state.home;$('cliInput').value=state.cli.path||'';uiText($('cliDetail'),`${state.cli.version}\n${state.cli.message}\n${state.source}`);$('settingsDialog').showModal();};
$('rescan').onclick=async()=>{try{$('rescan').disabled=true;await api('/api/scan',{});$('diagnosticsDialog').close();}catch(e){error(e);}finally{$('rescan').disabled=false;}};
$('openDiagnostics').onclick=()=>{diagnosticPage=0;$('diagnosticsDialog').showModal();renderDiagnostics();};
$('closeDiagnostics').onclick=()=>$('diagnosticsDialog').close();
$('diagnosticPrev').onclick=()=>{diagnosticPage--;renderDiagnostics();$('diagnosticRows').scrollTop=0;};
$('diagnosticNext').onclick=()=>{diagnosticPage++;renderDiagnostics();$('diagnosticRows').scrollTop=0;};
for(const id of ['diagnosticKind','diagnosticSearch'])$(id).addEventListener(id==='diagnosticKind'?'change':'input',()=>{diagnosticPage=0;renderDiagnostics();$('diagnosticRows').scrollTop=0;});
$('closeSettings').onclick=()=>$('settingsDialog').close();
$('saveSettings').onclick=async()=>{$('saveSettings').disabled=true;try{await api('/api/settings',{home:$('homeInput').value,cli:$('cliInput').value});selected.clear();$('settingsDialog').close();}catch(e){uiText($('settingsError'),e.message);$('settingsError').hidden=false;}finally{$('saveSettings').disabled=false;}};
$('plan').onclick=async()=>{try{activePlan=await api('/api/plan',{ids:[...selected]});$('planDialog').classList.remove('show-result');uiText(document.querySelector('.plan-header h2'),msg('permanent_deletion'));$('execute').hidden=false;$('retryPlan').hidden=true;$('planBody').replaceChildren();for(const e of activePlan.entries){const box=element('div',undefined,'planentry');box.append(content('b',e.title),element('span',e.included_descendant?'（随父会话删除的子会话）':''),element('p',msg('plan_file_count',{id:e.id,count:e.files.length,size:fmt(e.bytes)})));for(const note of e.warnings||[])box.append(element('p',note,'plan-note'));for(const reason of e.blockers)box.append(element('p',reason,'warning'));$('planBody').append(box);}for(const reason of activePlan.blockers)$('planBody').append(element('p',reason,'warning'));const ownEntries=activePlan.entries.filter(e=>selected.has(e.id)),relatedEntries=activePlan.entries.filter(e=>!selected.has(e.id));uiText($('planSummary'),sizeSummary(ownEntries.reduce((s,e)=>s+e.bytes,0),relatedEntries.reduce((s,e)=>s+e.bytes,0),relatedEntries.length,activePlan.entries.length));uiText($('execute'),msg('delete_count',{count:activePlan.entries.length}));$('execute').disabled=!activePlan.allowed;uiText($('result'),'');$('planDialog').showModal();}catch(e){error(e);}};
$('closeProblems').onclick=()=>$('problemDialog').close();
$('retryPlan').onclick=()=>{$('planDialog').close();$('plan').click();};
$('closePlan').onclick=()=>{if(!deleting)$('planDialog').close();};
$('planDialog').addEventListener('cancel',e=>{if(deleting)e.preventDefault();});
window.addEventListener('beforeunload',e=>{if(deleting||databaseBusy){e.preventDefault();e.returnValue='';}});
let progressTimer,localDelete=false,observedDelete=false,completionHandled=false,completedStarted=0,currentStarted=0;
function finishDelete(result, failure=''){
 if(completionHandled)return;
 completionHandled=true;completedStarted=Math.max(completedStarted,currentStarted,Date.now()/1000);
 localDelete=false;observedDelete=false;deleting=false;activePlan=null;
 $('stopDelete').hidden=true;$('deleteProgress').hidden=true;$('closePlan').disabled=false;$('execute').disabled=true;
 const results=result?.results||[];
 for(const item of results)if(item.status==='成功')deletedIds.add(item.id);
 if(state){
  state={...state,rows:state.rows.filter(r=>!deletedIds.has(r.id))};
  for(const id of deletedIds)selected.delete(id);
  if(deletedIds.has(focusId))clearPreview();
  uiText($('size'),fmt(state.rows.reduce((sum,row)=>sum+row.size,0)));
  render();
 }
 const allSucceeded=!!result&&results.length>0&&results.every(r=>r.status==='成功');
 if(allSucceeded){selected.clear();selection();$('planDialog').close();}
 else{
  if(result)selected=new Set(results.filter(r=>r.status!=='成功').map(r=>r.id));
  selection();$('planDialog').classList.add('show-result');$('planBody').replaceChildren();$('execute').hidden=true;$('retryPlan').hidden=!selected.size;
  uiText(document.querySelector('.plan-header h2'),failure?'删除未执行':'清理结果');
  I18N.unbind($('result'));$('result').replaceChildren();
  if(failure)$('result').append(element('p','删除未完成：'+failure));
  else{
   $('result').append(element('p',result?.note||'删除已结束'));
   for(const r of results){const item=element('div',undefined,'result-item');item.append(content('b',r.title||r.id),element('strong',r.status,'result-status'));for(const detail of [r.message,r.verification_error])if(detail)item.append(element('p',detail));for(const f of r.leftovers||[])item.append(content('p',f.path));$('result').append(item);}
  }
  uiText($('planSummary'),failure?'本次请求未完成，可重新检查后再试。':`成功 ${results.filter(r=>r.status==='成功').length} 项 · 部分完成 ${results.filter(r=>r.status==='部分完成').length} 项 · 失败 ${results.filter(r=>r.status==='失败').length} 项 · 未执行 ${results.filter(r=>r.status==='未执行').length} 项`);
  $('result').scrollIntoView({block:'start'});
 }
 // Unlock immediately; refreshing the list is independent of deletion completion.
 api('/api/scan',{}).catch(e=>error('列表更新失败：'+e.message));
}
async function pollDeleteProgress(){
 try{const p=await api('/api/delete-progress');
  if(p.running&&p.started>completedStarted){
   currentStarted=p.started;observedDelete=true;deleting=true;completionHandled=false;
   if(!$('planDialog').open){$('planBody').replaceChildren();uiText($('result'),'');uiText($('planSummary'),msg('reconnecting_to_the_running_deletion'));$('planDialog').showModal();}
   $('execute').disabled=true;$('closePlan').disabled=true;selection();
   $('stopDelete').hidden=!p.can_stop;$('stopDelete').disabled=!!p.stop_requested;uiText($('stopDelete'),p.stop_requested?'正在停止…':'停止后续删除');
   $('deleteProgress').hidden=false;$('deleteProgressBar').max=p.total||1;$('deleteProgressBar').value=p.done;
   const seconds=Math.max(0,Math.floor(Date.now()/1000-p.started));
   uiText($('deleteProgressText'),`已处理 ${p.done} / ${p.total} · ${p.phase} · 已用时 ${seconds} 秒`+(p.title?' · '+p.title:''));
  }else if(!p.running&&(observedDelete||(localDelete&&p.started>completedStarted))){
   currentStarted=p.started||currentStarted;finishDelete(p.result,p.error||'');
  }
 }catch{}finally{progressTimer=setTimeout(pollDeleteProgress,500);}
}
pollDeleteProgress();
$('stopDelete').onclick=async()=>{try{$('stopDelete').disabled=true;await api('/api/delete-stop',{});uiText($('stopDelete'),msg('stopping'));}catch(e){error(e);$('stopDelete').disabled=false;}};
$('execute').onclick=async()=>{
 if(deleting||!activePlan?.allowed)return;
 const planToken=activePlan.token;
 completionHandled=false;completedStarted=Date.now()/1000;localDelete=true;deleting=true;
 $('deleteProgress').hidden=false;$('deleteProgressBar').value=0;uiText($('deleteProgressText'),msg('preparing_deletion'));
 $('execute').disabled=true;$('closePlan').disabled=true;uiText($('result'),msg('deleting_sessions_and_verifying_results'));
 try{const result=await api('/api/delete',{token:planToken,confirmed:true});finishDelete(result);}
 catch(e){if(!completionHandled){
  try{const p=await api('/api/delete-progress');if(p.running){currentStarted=p.started;observedDelete=true;return;}finishDelete(p.result,p.error||e.message);}
  catch{localDelete=false;uiText($('result'),msg('connection_lost_checking_deletion_status'));}
 }}
};

poll();

// Resize the preview without changing the independent scrolling panes.
(()=>{
 const workspace=document.querySelector('.workspace'),preview=workspace.querySelector('aside');
 const handle=element('div',undefined,'preview-resizer');
 handle.tabIndex=0;handle.setAttribute('role','separator');I18N.attribute(handle,'aria-label','调整会话预览宽度');handle.setAttribute('aria-orientation','vertical');I18N.attribute(handle,'title','拖动调整预览宽度；双击恢复默认');workspace.append(handle);
 let preferred=null,dragging=false;
 try{const saved=Number(localStorage.getItem('previewWidth'));if(saved>=260)preferred=saved;}catch{}
 const limits=()=>[260,Math.max(260,Math.min(900,workspace.clientWidth-(workspace.classList.contains('with-project-nav')?560:380)))];
 function layout(){
  if(innerWidth<=800)return;
  const [min,max]=limits();
  if(preferred!==null)workspace.style.setProperty('--preview-width',Math.max(min,Math.min(max,preferred))+'px');
  const width=Math.round(preview.getBoundingClientRect().width),gap=parseFloat(getComputedStyle(workspace).columnGap)||0;
  handle.style.left=(preview.offsetLeft-gap/2)+'px';
  handle.setAttribute('aria-valuemin',min);handle.setAttribute('aria-valuemax',max);handle.setAttribute('aria-valuenow',width);I18N.attribute(handle,'aria-valuetext',msg('pixel_value',{count:width}));
 }
 function save(){try{if(preferred===null)localStorage.removeItem('previewWidth');else localStorage.setItem('previewWidth',preferred);}catch{}}
 function change(width){const [min,max]=limits();preferred=Math.max(min,Math.min(max,width));layout();}
 handle.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();dragging=true;handle.setPointerCapture(e.pointerId);document.body.classList.add('resizing-preview');});
 handle.addEventListener('pointermove',e=>{if(dragging)change(workspace.getBoundingClientRect().right-e.clientX-(parseFloat(getComputedStyle(workspace).columnGap)||0)/2);});
 function finish(){if(!dragging)return;dragging=false;document.body.classList.remove('resizing-preview');save();}
 handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);handle.addEventListener('lostpointercapture',finish);
 handle.addEventListener('dblclick',()=>{preferred=null;workspace.style.removeProperty('--preview-width');save();layout();});
 handle.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home'].includes(e.key))return;e.preventDefault();if(e.key==='Home'){preferred=null;workspace.style.removeProperty('--preview-width');layout();}else change(preview.getBoundingClientRect().width+(e.key==='ArrowLeft'?20:-20));save();});
 new ResizeObserver(layout).observe(workspace);new ResizeObserver(layout).observe(preview);new MutationObserver(layout).observe(workspace,{attributes:true,attributeFilter:['class']});window.addEventListener('resize',layout);layout();
})();

window.addEventListener('cleaner-language-change',()=>{if(state){render();renderDiagnostics();}});
