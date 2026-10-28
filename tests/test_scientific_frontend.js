// Executes production functions in Node; no browser, network or DB writes.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync('index.html','utf8');
const ctx=vm.createContext({console, window:{}, DEFAULT_INP:'', esc:x=>String(x), $:()=>({})});
vm.runInContext(html.slice(html.indexOf('const KB_SECTIONS ='),html.indexOf('function renderInpValidation(')),ctx);
vm.runInContext(html.slice(html.indexOf('function symbolsToXyz('),html.indexOf('/* 从研究体系库',html.indexOf('function symbolsToXyz('))),ctx);
vm.runInContext(html.slice(html.indexOf('const CHART_C ='),html.indexOf('if (window.Chart)',html.indexOf('const CHART_C ='))),ctx);
vm.runInContext(`
if (!CHART_C.dim || !CHART_C.danger) throw Error('chart colors missing');
const valid=analyzeInp('&FORCE_EVAL\\n&SUBSYS\\n&COORD\\nUNIT angstrom\\nSCALED F\\nO 0 1 2\\nO1 1D-2 .3 -4\\n&END COORD\\n&END SUBSYS\\n&END FORCE_EVAL');
if(valid.err) throw Error(JSON.stringify(valid.issues));
if(!analyzeInp('&COORD\\nO nan 0 0\\n&END COORD').err) throw Error('invalid coordinate accepted');
if(!analyzeInp('&DFT\\nO 0 1 2\\n&END DFT').err) throw Error('coordinate accepted outside COORD');
`,ctx);
console.log('PASS: coordinate context, fallback keywords and chart colors');
const view=process.env.CP2KVIEW_ROOT;
if(!view){console.log('SKIP: external CP2K viewer parser (set CP2KVIEW_ROOT to enable)');process.exit(0);}
vm.runInContext(fs.readFileSync(view+'/src/parsers.js','utf8'),ctx);
const inp='&SUBSYS\n&CELL\nA [angstrom] 8.1459 0 0\nB [angstrom] 4.07295 7.05455633668766 0\nC [angstrom] 0 0 44.6011\n&END CELL\n&COORD\nUNIT angstrom\nSCALED F\nO 0 1 2\n&END COORD\n&END SUBSYS';
const inputFrame=ctx.window.CP2KParsers.parseFile('regression.inp',inp).frames[0];
assert(inputFrame.cell,'explicit angstrom units must retain viewer cell');
assert.equal(inputFrame.cell.B[0],4.07295);assert.equal(inputFrame.atoms.length,1);
const parse=text=>ctx.window.CP2KParsers.parseFile('regression.out',text).analysis;
let a=parse('PROGRAM STARTED AT old\n[ABORT] old failure\nWARNING: old warning\nPROGRAM STARTED AT new\nTotal energy: -10.0\nTotal energy (extrapolated to T->0): -9.99283388167675\nENERGY| Total FORCE_EVAL ( QS ) energy [hartree]: -10.0\nThe number of warnings for this run is : 0\nPROGRAM ENDED AT now');
assert.equal(a.warnings.length,0);assert.equal(a.errors.length,0);assert.equal(a.reportedWarningCount,0);assert.equal(a.normalEnd,true);
let fields=a.energySnapshots.at(-1).fields;
assert(Math.abs(fields.extrapolated_t0.value_eV-fields.total_energy.value_eV-(-9.99283388167675+10)*27.211386245988)<1e-9);
assert(fields.total_energy.line>4);assert(fields.extrapolated_t0.source.includes('extrapolated'));
a=parse('WARNING: actual warning\nThe number of warnings for this run is : 1');
assert.equal(a.warnings.length,1);assert.equal(a.reportedWarningCount,1);
a=parse('Total energy: -10\nTotal energy (extrapolated to T->0): -9.99\nTotal energy: -11');
assert(!a.energySnapshots.at(-1).fields.extrapolated_t0,'must not reuse stale T0 energy');
console.log('PASS: coordinate context, fallback keywords, chart colors, warning counts, last-run isolation, energy units and missing-field isolation');
