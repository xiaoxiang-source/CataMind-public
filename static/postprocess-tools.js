/* Unified postprocessing UI; execution success is not scientific acceptance. */
let ppCatalog = null, ppTimer = null, ppLoading = false;
const ppLabels = {total_cube:'整体电子密度 cube',slab_cube:'冻结几何的基底 cube',ads_cube:'冻结几何的吸附物 cube',density_cube:'待分区电子密度 cube',reference_cube:'分区参考密度 cube（可选）',acf_file:'Bader ACF.dat',directory:'PDOS 文件目录',output_file:'CP2K 输出文件'};
const ppStates = {queued:'等待执行',running:'运行中',done:'执行完成 · 待科学验收',needs_review:'检查未通过 · 待复核',failed:'执行失败',interrupted:'重启中断'};
async function loadPostprocessTools() {
  const r = await api('/api/postprocess/tools');
  if (!r.ok) { $('#pp-catalog').textContent = r.error || '工具目录加载失败'; return; }
  ppCatalog = r.data;
  $('#pp-catalog').innerHTML = ppCatalog.tools.map(t => '<div class="card"><h3>' + esc(t.label) + '</h3><p class="hint">' + esc(t.description) + '</p><div class="hint">' + esc(t.reason || '可执行 · 结果仅供预览') + '</div><button class="btn sm" ' + (t.available ? '' : 'disabled') + ' onclick="openPostprocessTool(\'' + t.id + '\')">选择输入</button></div>').join('');
  $('#pp-planned').innerHTML = ppCatalog.planned.map(t => '<p><b>' + esc(t.label) + '（规划中）</b> · ' + esc(t.requirement) + '</p>').join('');
  loadPostprocessJobs();
}
function openPostprocessTool(id) {
  const tool = ppCatalog?.tools.find(t=>t.id===id); if (!tool || !tool.available) return;
  let body = '<p class="hint">' + esc(tool.description) + '</p>';
  [...tool.inputs,...(tool.optional_inputs||[])].forEach(k=>{
    body+=`<div class="field"><label>${esc(ppLabels[k]||k)}</label><input id="pp-${k}" placeholder="请选择文件，也可填写服务器绝对路径"><div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap"><button class="btn sm" onclick="ppOpenPicker('${k}')">选择服务器${k==='directory'?'目录':'文件'}</button><button class="btn sm" onclick="ppLocalFiles('${k}')">${k==='directory'?'上传本地 PDOS 文件（多选）':'上传本地文件'}</button></div></div>`;
  });
  body+='<p class="hint">本地上传每批最多 32 MiB、100 个文件。大文件请从服务器选择。</p>';
  if (id==='pdos') body+='<div class="grid g2">' + [['sigma','展宽 σ（eV）',.05],['de','能量步长（eV）',.01],['emin','相对费米能下界（eV）',-15],['emax','上界（eV）',5]].map(([key,label,v])=>'<div class="field"><label>'+label+'</label><input type="number" step="any" id="pp-'+key+'" value="'+v+'"></div>').join('')+'</div>';
  if (id.startsWith('bader')) {
    body+='<div class="field"><label>每个原子的参考电子数（按原子顺序，逗号分隔；留空只输出盆电子数）</label><textarea id="pp-valence" placeholder="例如：按赝势实际价电子数填写"></textarea></div><div class="field"><label>预期总电子数（可选）</label><input type="number" step="any" id="pp-electrons"></div>';
    if (id==='bader_import') body+='<label><input type="checkbox" id="pp-order"> 已核对 ACF.dat 与 cube 的原子顺序一致</label>';
  }
  if (id.startsWith('bader')||id==='density_difference') body+='<p><label><input type="checkbox" id="pp-unit"> 已确认密度单位为 e/Bohr³，cube 坐标和网格向量为 Bohr</label></p>';
  openModal(tool.label,body,'<button class="btn ghost" onclick="closeModal()">取消</button><button class="btn primary" id="pp-submit" onclick="submitPostprocessTool(\''+id+'\')">开始分析</button>');
}
function postprocessConfig(id) {
  const spec=ppCatalog.tools.find(t=>t.id===id), inputs={},params={};
  [...spec.inputs,...(spec.optional_inputs||[])].forEach(k=>{const v=$('#pp-'+k).value.trim(); if(v)inputs[k]=v; else if(spec.inputs.includes(k))throw Error('请填写 '+ppLabels[k]);});
  if(id==='pdos') {params.sigma=Number($('#pp-sigma').value);params.de=Number($('#pp-de').value);params.erange=[Number($('#pp-emin').value),Number($('#pp-emax').value)];}
  if(id.startsWith('bader')) {
    const val=$('#pp-valence').value.trim(), total=$('#pp-electrons').value.trim();
    if(val){params.valence_electrons=val.split(/[,，\s]+/).map(Number);if(params.valence_electrons.some(v=>!Number.isFinite(v)||v<0))throw Error('参考电子数需为非负有限数值');}
    if(total){params.expected_electrons=Number(total);if(!Number.isFinite(params.expected_electrons))throw Error('总电子数格式错误');}
    if(id==='bader_import'){if(!$('#pp-order').checked)throw Error('请核对原子顺序');params.atom_order_confirmed=true;}
  }
  if(id.startsWith('bader')||id==='density_difference'){if(!$('#pp-unit').checked)throw Error('请先确认密度与长度单位');params.density_unit='e/bohr^3';}
  return {tool:id,inputs,params};
}
async function submitPostprocessTool(id) {
  if(ppUploading){toast('文件正在上传，请稍候','err');return;}
  let config;try{config=postprocessConfig(id);}catch(e){toast(e.message,'err');return;}
  const btn=$('#pp-submit');btn.disabled=true;
  try{const r=await api('/api/postprocess/tools/run',{method:'POST',body:config});if(!r.ok){toast(r.error,'err');return;}closeModal();toast('已创建后处理任务');loadPostprocessJobs();}
  finally{btn.disabled=false;}
}
async function loadPostprocessJobs() {
  clearTimeout(ppTimer);
  if(ppLoading||!$('#mod-postprocessing').classList.contains('active')||document.hidden)return;
  ppLoading=true;
  try {
    const r=await api('/api/postprocess/tools/jobs');
    if(!r.ok){$('#pp-jobs').textContent=r.error||'加载失败';return;}
    const rows=r.data||[];
    $('#pp-jobs').innerHTML=rows.length?'<table class="tbl"><thead><tr><th>工具</th><th>状态</th><th>创建时间</th><th>结果</th></tr></thead><tbody>'+rows.map(j=>'<tr><td>'+esc(ppCatalog?.tools.find(t=>t.id===j.tool)?.label||j.tool)+'</td><td>'+esc(ppStates[j.status]||j.status)+'</td><td>'+esc(j.created_at)+'</td><td><button class="btn sm" onclick="showPostprocessResult(\''+j.id+'\')">查看</button></td></tr>').join('')+'</tbody></table>':'<p class="hint">暂无后处理任务。已有 PDOS 验收记录仍在计算任务模块保留。</p>';
    if(rows.some(j=>['running','queued'].includes(j.status)))ppTimer=setTimeout(loadPostprocessJobs,4000);
  }finally{ppLoading=false;}
}
async function showPostprocessResult(id) {
  const response=await api('/api/postprocess/tools/jobs/'+id);if(!response.ok){toast(response.error,'err');return;}
  const j=response.data,r=j.result;
  let body='<p>'+esc(ppStates[j.status]||j.status)+'</p>';
  if(j.error)body+='<p class="hint warn">'+esc(j.error)+'</p>';
  if(r){
    body+='<p class="hint warn">当前为结果预览，尚未科学验收，不自动写入训练集。</p>';
    for(const [key,label] of [['density_integral_e','密度积分（e）'],['accumulation_e','电子积累量（e）'],['depletion_e','电子耗尽量（e）'],['last_energy_hartree','最后电子能（Hartree）'],['execution_status','程序结束状态']])if(r[key]!=null)body+='<p><b>'+label+'</b>：'+esc(r[key])+'</p>';
    if(r.atoms)body+='<table class="tbl"><thead><tr><th>原子序号</th><th>盆电子数</th><th>净电荷（e）</th></tr></thead><tbody>'+r.atoms.slice(0,100).map(a=>'<tr><td>'+a.index+'</td><td>'+a.basin_electrons+'</td><td>'+(a.net_charge_e??'未提供参考电子数')+'</td></tr>').join('')+'</tbody></table><p class="hint">最多展示前 100 个原子，完整结果见 JSON。</p>';
    body+=(r.checks||[]).map(c=>'<p>'+esc(c.name)+'：'+(c.ok===true?'通过':c.ok===false?'未通过':'缺少信息')+'</p>').join('');
    body+=(r.warnings||[]).map(w=>'<p class="hint">'+esc(typeof w==='string'?w:JSON.stringify(w))+'</p>').join('');
    if(r.tool==='pdos')body+='<details><summary>PDOS 摘要</summary><pre style="white-space:pre-wrap">'+esc(JSON.stringify(r.summary||r.elements,null,2))+'</pre></details>';
    body+='<details><summary>输入来源与参数</summary><pre style="white-space:pre-wrap">'+esc(JSON.stringify({parameters:r.parameters,sources:r.sources},null,2))+'</pre></details>';
    body+=['result.json',...(r.artifacts||[]).map(a=>a.name)].map(name=>'<button class="btn sm" onclick="downloadPostprocessArtifact(\''+id+'\',\''+name+'\')">下载 '+esc(name)+'</button>').join(' ');
  }
  openModal('后处理结果',body,'<button class="btn" onclick="closeModal()">关闭</button>');
}
async function downloadPostprocessArtifact(id,name){
  const r=await authFetch('/api/postprocess/tools/jobs/'+encodeURIComponent(id)+'/'+encodeURIComponent(name));
  if(!r.ok){toast('下载失败','err');return;}
  const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&$('#mod-postprocessing')?.classList.contains('active'))loadPostprocessJobs();});

let ppPicker = null, ppPickerRequest = 0, ppUploading = false;
const ppAccept = {directory:'.pdos',acf_file:'.dat',output_file:'.out,.log,.output'};
function ppOpenPicker(field) {
  if(ppUploading){toast('文件正在上传，请稍候','err');return;}
  ppPicker={field,entries:[],path:'',parent:null};
  openModal('选择服务器'+(field==='directory'?' PDOS 目录':'文件'),
    '<p class="hint">进入目录后选择文件；PDOS 请选择包含 .pdos 文件的目录。</p><div style="display:flex;gap:8px"><button class="btn sm" onclick="ppBrowse(\'\')">计算目录</button><button class="btn sm" id="pp-parent" disabled onclick="ppBrowse(ppPicker.parent)">上一级</button><button class="btn sm" id="pp-select-dir" disabled onclick="ppSelectPath(ppPicker.path)">使用当前目录</button></div><p id="pp-location" style="overflow-wrap:anywhere"></p><div id="pp-browser">正在加载…</div>',
    '<button class="btn" onclick="closeModal()">取消</button>',{stack:true});
  ppBrowse('');
}
async function ppBrowse(path) {
  const panel=$('#pp-browser'), state=ppPicker, request=++ppPickerRequest;
  $('#pp-select-dir').disabled=true;$('#pp-parent').disabled=true;
  panel.textContent='正在加载…';
  try {
    const r=await api('/api/postprocess/tools/browse',{method:'POST',body:{field:state.field,path}});
    if(request!==ppPickerRequest||$('#pp-browser')!==panel)return;
    if(!r.ok){panel.textContent=r.error||'目录加载失败';return;}
    Object.assign(state,r.data);
    $('#pp-location').textContent=state.path||'选择计算目录';
    $('#pp-parent').disabled=state.parent===null;
    $('#pp-select-dir').disabled=state.field!=='directory'||!state.path;
    panel.innerHTML=(state.truncated?'<p class="hint warn">目录超过 10000 项，仅列出部分内容。可返回表单直接填写路径。</p>':'')+
      (state.entries.length?state.entries.map((e,i)=>'<div style="padding:5px;overflow-wrap:anywhere"><button class="btn sm" onclick="ppPickEntry('+i+')" '+(state.field==='directory'&&!e.directory?'disabled':'')+'>'+ (e.directory?'📁 进入':'选择')+'</button> '+esc(e.name)+'</div>').join(''):'<p class="hint">此目录没有符合类型的文件或子目录。</p>');
  } catch(e) {if($('#pp-browser')===panel)panel.textContent='加载失败：'+e.message;}
}
function ppPickEntry(index) {
  const entry=ppPicker.entries[index];if(!entry)return;
  if(entry.directory)ppBrowse(entry.path);else if(ppPicker.field!=='directory')ppSelectPath(entry.path);
}
function ppSelectPath(path) {
  if(!path)return;
  const field=ppPicker.field;closeModal();
  const input=$('#pp-'+field);if(input)input.value=path;
}
function ppLocalFiles(field) {
  if(ppUploading){toast('文件正在上传，请稍候','err');return;}
  const picker=document.createElement('input');picker.type='file';picker.accept=ppAccept[field]||'.cube';picker.multiple=field==='directory';
  picker.onchange=()=>ppUploadFiles(field,Array.from(picker.files||[]));picker.click();
}
async function ppUploadFiles(field,files) {
  if(!files.length||ppUploading)return;
  if(files.length>100||files.reduce((n,f)=>n+f.size,0)>32*1024*1024){toast('每批最多 100 个文件、32 MiB；大文件请从服务器选择','err');return;}
  const input=$('#pp-'+field),btn=$('#pp-submit');ppUploading=true;if(btn)btn.disabled=true;
  toast('正在上传所选文件…');
  try {
    const payload=[];
    for(const file of files){
      const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('无法读取 '+file.name));reader.readAsDataURL(file);});
      payload.push({name:file.name,content_b64:content});
    }
    const r=await api('/api/postprocess/tools/upload',{method:'POST',body:{field,files:payload}});
    if(!r.ok)throw Error(r.error||'上传失败');
    if($('#pp-'+field)===input)input.value=r.data.path;
    toast('已上传 '+r.data.count+' 个文件'+($('#pp-'+field)===input?'，输入已自动填入':'，可在服务器选择器的上传目录中找到'));
  }catch(e){toast(e.message,'err');}
  finally{ppUploading=false;if(btn)btn.disabled=false;}
}
