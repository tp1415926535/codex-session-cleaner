'use strict';
(()=>{
 let report=null,plan=null,observed=false,starting=false;
 const dialog=$('databaseDialog');
 const description={'thread_history_1.sqlite':'db_history','logs_2.sqlite':'db_logs','state_5.sqlite':'db_state'};
 function showError(e){$('dbError').hidden=false;uiText($('dbError'),msg('db_error',{detail:e.message||String(e)}));}
 function invalidate(){plan=null;$('dbPlan').hidden=true;$('dbExecute').hidden=true;$('dbReview').hidden=false;}
 function controls(busy){
  databaseBusy=busy;$('dbClose').disabled=busy;$('dbReview').disabled=busy||!report;
  $('dbExecute').disabled=busy;$('dbLogDays').disabled=busy;$('dbStop').hidden=!busy;
  for(const input of dialog.querySelectorAll('input[type=checkbox]'))input.disabled=busy||input.dataset.invalid==='true';
  if(state)selection();
 }
 async function inspect(){
  $('dbLoading').hidden=false;uiText($('dbLoading'),msg('db_loading'));$('dbError').hidden=true;report=null;controls(false);
  try{
   report=await api('/api/database-inspect');$('dbTable').replaceChildren();
   const table=element('table'),head=element('thead'),tr=element('tr');
   for(const key of ['db_choose','db_database','db_disk','db_free'])tr.append(element('th',msg(key)));
   head.append(tr);table.append(head);const body=element('tbody');
   for(const db of report.databases){
    const row=element('tr'),checkCell=element('td'),check=element('input');check.type='checkbox';check.value=db.name;
    check.dataset.invalid=String(!db.exists||!!db.error);check.disabled=check.dataset.invalid==='true';check.checked=!check.disabled;
    I18N.attribute(check,'aria-label',msg('db_select',{name:db.name}));check.onchange=invalidate;checkCell.append(check);
    const name=element('td');name.append(content('b',db.name),element('small',msg(description[db.name]),'db-description'));
    row.append(checkCell,name,element('td',db.exists&&!db.error?fmt(db.bytes+db.wal_bytes):msg('db_unavailable')),element('td',db.free_bytes!==undefined?fmt(db.free_bytes):'—'));
    if(db.error)name.append(element('small',db.error,'db-description'));body.append(row);
   }
   table.append(body);$('dbTable').append(table);
   const history=report.databases.find(d=>d.name==='thread_history_1.sqlite');
   $('dbUnmatched').hidden=!history?.unmatched_sessions;
   if(history?.unmatched_sessions)uiText($('dbUnmatched'),msg('db_unmatched',{count:history.unmatched_sessions,records:history.unmatched_records?.thread_items||0}));
  }catch(e){showError(e);}finally{$('dbLoading').hidden=true;controls(databaseBusy);}
 }
 $('openDatabases').onclick=()=>{dialog.showModal();if(!databaseBusy){invalidate();$('dbResults').hidden=true;inspect();}};
 $('dbLogDays').onchange=invalidate;
 $('dbClose').onclick=()=>{if(!databaseBusy)dialog.close();};
 dialog.addEventListener('cancel',e=>{if(databaseBusy)e.preventDefault();});
 $('dbReview').onclick=async()=>{
  $('dbError').hidden=true;$('dbReview').disabled=true;
  try{
   const names=[...dialog.querySelectorAll('#dbTable input:checked')].map(input=>input.value);
   const log_days=$('dbLogDays').value===''?null:Number($('dbLogDays').value);
   plan=await api('/api/database-plan',{names,log_days});$('dbPlan').replaceChildren();
   $('dbPlan').append(element('p',msg('db_plan',{count:plan.entries.length,size:fmt(plan.entries.reduce((sum,d)=>sum+d.free_bytes,0))})));
   for(const entry of plan.entries)$('dbPlan').append(element('p',msg('db_plan_entry',{name:entry.name,size:fmt(entry.free_bytes)})));
   $('dbPlan').append(element('p',log_days===null?msg('db_no_log_delete'):msg('db_log_delete',{count:plan.entries.reduce((sum,d)=>sum+d.logs_to_delete,0),date:new Date(plan.cutoff*1000).toLocaleString(I18N.language==='en'?'en-US':'zh-CN')})));
   $('dbPlan').hidden=false;$('dbExecute').hidden=false;$('dbReview').hidden=true;$('dbPlan').scrollIntoView({block:'nearest'});
  }catch(e){showError(e);}finally{$('dbReview').disabled=false;}
 };
 $('dbExecute').onclick=async()=>{
  if(!plan||databaseBusy)return;starting=true;controls(true);$('dbError').hidden=true;$('dbProgress').hidden=false;
  uiText($('dbProgressText'),msg('db_starting'));
  try{await api('/api/database-start',{token:plan.token,confirmed:true});observed=true;plan=null;}
  catch(e){showError(e);controls(false);$('dbProgress').hidden=true;}
  finally{starting=false;}
 };
 $('dbStop').onclick=async()=>{try{$('dbStop').disabled=true;await api('/api/database-stop',{});}catch(e){showError(e);$('dbStop').disabled=false;}};
 function results(p){
  $('dbResults').replaceChildren();$('dbResults').hidden=false;
  $('dbResults').append(element('h3',msg('db_finished',{size:fmt(p.results.reduce((sum,r)=>sum+r.released,0))})));
  for(const r of p.results){const box=element('div',undefined,'db-result');box.append(content('b',r.name),element('p',msg('db_result',{status:I18N.message('db_status_'+r.status),logs:r.deleted_logs,size:fmt(r.released)})));if(r.error)box.append(element('p',msg('db_error',{detail:r.error})));$('dbResults').append(box);}
  $('dbResults').scrollIntoView({block:'nearest'});
 }
 async function progress(){
  try{const p=await api('/api/database-progress');
   if(p.running){
    observed=true;controls(true);
    if(!dialog.open){$('settingsDialog').close();$('planDialog').close();dialog.showModal();}
    $('dbProgress').hidden=false;$('dbProgressBar').max=p.total||1;$('dbProgressBar').value=p.done;
    uiText($('dbProgressText'),msg('db_progress',{done:p.done,total:p.total,name:p.database,phase:msg(p.stopping?'db_stopping':'db_phase_'+p.phase)}));
    $('dbStop').disabled=!!p.stopping;
   }else if(observed&&!starting){observed=false;controls(false);$('dbProgress').hidden=true;invalidate();results(p);inspect();}
  }catch{}finally{setTimeout(progress,1000);}
 }
 progress();
})();
