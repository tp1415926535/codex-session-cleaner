'use strict';
// Locale resources are independent JSON catalogs. Session content is never translated.
const I18N=(()=>{
 const supported=['zh-CN','en'];
 const preferredLanguage=navigator.languages?.[0]||navigator.language||'en';
 let language=/^zh(?:-|$)/i.test(preferredLanguage)?'zh-CN':'en',catalogs={},patterns=[],exact=new Map();
 try{const saved=localStorage.getItem('cleaner.language');if(supported.includes(saved))language=saved;}catch{}
 const textBindings=new Map(),attributeBindings=new Map();
 let cleanupQueued=false;
 function cleanup(){if(cleanupQueued)return;cleanupQueued=true;queueMicrotask(()=>{cleanupQueued=false;for(const node of textBindings.keys())if(!node.isConnected)textBindings.delete(node);for(const node of attributeBindings.keys())if(!node.isConnected)attributeBindings.delete(node);});}

 function format(key,values={}){
  if(values.count!==undefined&&new Intl.PluralRules(language).select(Number(values.count))==='one'&&catalogs[language]?.[key+'_one'])key+='_one';
  const template=catalogs[language]?.[key]??catalogs['zh-CN']?.[key]??key;
  return template.replace(/\{(\w+)\}/g,(match,name)=>Object.hasOwn(values,name)?(values[name]?.i18nKey?format(values[name].i18nKey,values[name].values):String(values[name])):match);
 }
 // Adapter for existing Chinese backend messages. Only complete catalog templates match;
 // identifiers, titles and paths captured inside a message are kept verbatim.
 function sourceText(source,depth=0){
  source=String(source??'');if(language==='zh-CN'||depth>8)return source;
  const key=exact.get(source);if(key)return format(key);
  const trimmed=source.trim();if(trimmed!==source&&exact.has(trimmed))return source.replace(trimmed,format(exact.get(trimmed)));
  for(const entry of patterns){const match=entry.pattern.exec(source);if(!match)continue;
   const values={};entry.names.forEach((name,i)=>{values[name]=['detail','phase','evidence'].includes(name)?sourceText(match[i+1],depth+1):match[i+1];});
   return format(entry.key,values);
  }
  if(source.includes('\n'))return source.split('\n').map(line=>sourceText(line,depth+1)).join('\n');
  return source;
 }
 function message(key,values={}){return {i18nKey:key,values};}
 function display(value){return value&&typeof value==='object'&&value.i18nKey?format(value.i18nKey,value.values):sourceText(value);}
 function text(node,value){textBindings.set(node,value);cleanup();node.textContent=display(value);return node;}
 function raw(node,value){textBindings.delete(node);node.removeAttribute?.('data-i18n');node.textContent=value??'';return node;}
 function attribute(node,name,value){if(!attributeBindings.has(node))attributeBindings.set(node,new Map());attributeBindings.get(node).set(name,value);cleanup();node.setAttribute(name,display(value));}
 function apply(){
  document.documentElement.lang=language;document.getElementById('language').value=language;
  for(const [node,value] of textBindings){if(!node.isConnected){textBindings.delete(node);continue;}node.textContent=display(value);}
  for(const [node,attrs] of attributeBindings){if(!node.isConnected){attributeBindings.delete(node);continue;}for(const [name,value] of attrs)node.setAttribute(name,display(value));}
  window.dispatchEvent(new Event('cleaner-language-change'));
 }
 function setLanguage(next){if(!supported.includes(next))return;language=next;try{localStorage.setItem('cleaner.language',language);}catch{}apply();}
 function initStatic(){
  for(const node of document.querySelectorAll('[data-i18n]'))if(!textBindings.has(node))text(node,message(node.dataset.i18n));
  for(const node of document.querySelectorAll('[data-i18n-title],[data-i18n-placeholder],[data-i18n-aria-label]'))for(const name of ['title','placeholder','aria-label']){const key=node.getAttribute('data-i18n-'+name);if(key)attribute(node,name,message(key));}
 }
 document.getElementById('language').value=language;
 document.getElementById('language').onchange=e=>setLanguage(e.target.value);
 const ready=Promise.all(supported.map(async lang=>{const response=await fetch('/locales/'+lang+'.json');if(!response.ok)throw new Error('Language resources could not be loaded');return [lang,await response.json()];})).then(entries=>{
  catalogs=Object.fromEntries(entries);
  for(const [key,template] of Object.entries(catalogs['zh-CN'])){
   if(['list_sessions','list_internal'].includes(key))continue;
   if(!template.includes('{')){exact.set(template,key);continue;}
   if(['file_detail','shared_file_detail'].includes(key))continue;
   const names=[];let last=0,regex='^';
   const escape=value=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
   for(const match of template.matchAll(/\{(\w+)\}/g)){regex+=escape(template.slice(last,match.index))+'([\\s\\S]*?)';names.push(match[1]);last=match.index+match[0].length;}
   regex+=escape(template.slice(last))+'$';patterns.push({key,names,pattern:new RegExp(regex),specificity:template.replace(/\{\w+\}/g,'').length});
  }
  patterns.sort((a,b)=>b.specificity-a.specificity);
  initStatic();apply();
 }).catch(()=>{document.getElementById('language').disabled=true;document.getElementById('language').title='Language resources unavailable / 语言资源加载失败';});
 return {message,format,text,raw,attribute,sourceText,unbind:node=>textBindings.delete(node),setLanguage,ready,get language(){return language;}};
})();
const uiText=(node,value)=>I18N.text(node,value);
const rawText=(node,value)=>I18N.raw(node,value);
const msg=(key,values)=>I18N.message(key,values);
