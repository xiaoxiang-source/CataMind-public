'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('index.html', 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(Boolean);
scripts.forEach(s => new vm.Script(s));
function extract(start, end) { return html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start))); }
const elements = {'#mi-map-c-0': {value: 'name'}, '#mi-map-c-1': {value: 'feature'}, '#mi-map-c-2': {value: 'target'}};
const ctx = vm.createContext({$: s => elements[s]});
vm.runInContext(extract('function collectCsvMapping(', 'async function mlParseColumns('), ctx);
const meta = {columns: [{name: 'name'}, {name: 'site_fukui'}, {name: 'target'}]};
assert.equal(JSON.stringify(ctx.collectCsvMapping('#mi-map', meta, 'ml')), JSON.stringify({name_col: 'name', target_col: 'target', feature_cols: ['site_fukui']}));
elements['#mi-map-c-1'].value = 'target';
assert.throws(() => ctx.collectCsvMapping('#mi-map', meta, 'ml'), /只能选择一列/);
elements['#mi-map-c-1'].value = 'feature'; elements['#mi-map-c-2'].value = 'ignore';
assert.throws(() => ctx.collectCsvMapping('#mi-map', meta, 'ml'), /目标/);
const calls = [];
const state = vm.createContext({window: {_mlCols: {sourceText: 'old'}}, $: s => ({'#mi-format': {value: 'csv'}, '#mi-data': {value: 'new'}, '#mi-train': {checked: false}}[s]),
  mlParseColumns: async () => calls.push('preview'), api: async () => calls.push('write'), toast: msg => calls.push(msg)});
vm.runInContext(extract('async function submitMlImport(', '/* =================='), state);
(async () => {
  await state.submitMlImport();
  assert(calls.includes('preview')); assert(!calls.includes('write'), 'edited CSV must require a new preview');
  assert(!html.includes('https://cdn.jsdelivr.net'), 'main UI must use bundled libraries');
  assert(html.includes('openResearchPanel(\'compare\')')); assert(html.includes('openResearchPanel(\'almd\')'));
  console.log('PASS: inline syntax, actual CSV selector mapping, duplicate/missing roles, stale preview guard, local assets and research entry points');
})().catch(e => { console.error(e); process.exitCode = 1; });
