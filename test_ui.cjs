// Interaction contract test without a browser, with minimal DOM/API doubles.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/app.html','utf8');
const elements=new Map();
class Element {
  constructor(){this.value='';this.dataset={};this.hidden=false;this.events={};this.style={};this.disabled=false;this.textContent='';this.innerHTML='';}
  addEventListener(name,fn){this.events[name]=fn;}
  setAttribute(){} focus(){} remove(){} appendChild(){}
  querySelector(s){return get(s);}
  getContext(){return {clearRect(){}};}
}
const get=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s)};
const controls={};
const root=new Element();
root.querySelector=get;
root.querySelectorAll=selector=>{
  if(selector==='[data-edit]'){
    controls.edit=[...get('#task-list').innerHTML.matchAll(/data-edit="(\d+)"/g)].map(m=>{let e=new Element();e.dataset.edit=m[1];return e});return controls.edit;
  }
  return [];
};
get('#pearl-reference-assets').textContent=html.match(/id="pearl-reference-assets">([\s\S]*?)<\/script>/)[1];
let tasks=[],next=1;
const errors=[];
const context={console,Promise,Array,Math,Date,Number,String,JSON,Set,Map,parseInt,
 document:{getElementById:()=>root,createElement:()=>new Element()},
 window:{Telegram:{WebApp:{initData:'test',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){}}}},
 Image:class {constructor(){this.complete=false}decode(){return Promise.resolve()}},
 crypto:{randomUUID:()=>String(next++)},alert:e=>errors.push(e),
 fetch:async(url,request)=>{
   const d=JSON.parse(request.body);
   if(d.op==='create')tasks.push({id:next++,title:d.title,category:d.category,done:false});
   if(d.op==='edit')Object.assign(tasks.find(t=>t.id===d.id),{title:d.title,category:d.category});
   if(d.op==='complete')tasks.find(t=>t.id===d.id).done=true;
   if(d.op==='restore')tasks.find(t=>t.id===d.id).done=false;
   if(d.op==='clear')tasks=tasks.filter(t=>!t.done);
   return {ok:true,json:async()=>({tasks})};
 },getComputedStyle:()=>({getPropertyValue:()=>'',color:'#20201e'}),matchMedia:()=>({matches:true,addEventListener(){}})};
const code=[...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)].filter(m=>!m[1].includes('application/json')&&!m[1].includes('src=')).map(m=>m[2]).join('\n');
vm.runInNewContext(code,context);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await settle();
 assert.equal(typeof get('#new-title').events.input,'function');
 get('#new-title').value='а'.repeat(41);get('#new-title').events.input();
 assert.equal(get('#new-title').value.length,40);assert.equal(get('#title-counter').textContent,'40 / 40');
 get('#new-title').value='Протереть зеркало';get('#new-category').value='bath';
 await get('#add-form').events.submit({preventDefault(){}});
 assert.equal(tasks.length,1);
 assert.match(get('#task-list').innerHTML,/data-lucide="pencil"/);
 controls.edit[0].events.click();
 assert.equal(get('#new-title').value,'Протереть зеркало');
 assert.equal(get('#new-category').value,'bath');
 assert.equal(get('#editor-heading').textContent,'Редактировать задачу');
 get('#new-title').value='Разобрать полку';get('#new-category').value='bed';
 await get('#add-form').events.submit({preventDefault(){}});
 assert.equal(tasks.length,1);assert.equal(tasks[0].title,'Разобрать полку');assert.equal(tasks[0].category,'bed');
 assert.deepEqual([...get('#new-category').innerHTML.matchAll(/value="([^"]+)"/g)].map(m=>m[1]),['bed','bath','hall','kitchen','balcony','sale','home','finance','shopping','documents']);
 assert.equal(errors.length,0);
 console.log('PASS: create → pencil → edit text/category → save; 40-character counter; category order');
})().catch(e=>{console.error(e);process.exitCode=1});
