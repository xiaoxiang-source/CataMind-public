'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('index.html', 'utf8');
const block = html.slice(html.indexOf('function openProcessMonitor()'), html.indexOf('function renderProcStats'));
let panel = {};
let shown = true;
let calls = 0;
let finish;
const elements = Object.fromEntries(['proc-sample-time','proc-stats','proc-jobs','proc-modules','proc-list'].map(id=>[id,{innerHTML:'old'}]));
const ctx = vm.createContext({
  $: selector => selector === '#proc-monitor' ? panel : selector === '#modal-bg' ? {classList:{contains:()=>shown}} : elements[selector.slice(1)],
  document:{hidden:false,getElementById:id=>elements[id]},
  api:()=>{calls++; return new Promise(resolve=>{finish=resolve;});},
  renderProcStats:()=>{},renderProcJobs:()=>{},renderProcList:()=>{},esc:String
});
vm.runInContext(block,ctx);
(async()=>{
  const pending=ctx.loadProcesses();
  await ctx.loadProcesses();
  assert.equal(calls,1,'requests coalesce');
  panel={}; // another modal replaced the old one
  finish({ok:true,data:{time:'old',summary:{},jobs:[],cp2k:[],modules:[]}});
  await pending;
  assert.equal(elements['proc-modules'].innerHTML,'old','stale response must not render');
  shown=false;
  await ctx.loadProcesses();
  assert.equal(calls,1,'closed modal must not sample');
  shown=true;
  const failed=ctx.loadProcesses();
  finish({ok:false,error:'offline'});
  await failed;
  assert.equal(elements['proc-modules'].innerHTML,'','failed sample clears stale data');
  assert(html.includes('role="button" tabindex="0" title="点击查看各模块进程资源"'));
  console.log('PASS: monitor request coalescing, stale response isolation, closed modal, error state, keyboard entry');
})().catch(e=>{console.error(e);process.exitCode=1;});
