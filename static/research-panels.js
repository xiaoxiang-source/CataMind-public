/* Native website panels for the existing ml_compare / al_multidim APIs. */
const RESEARCH = {
  compare: {title: '多模型对比', start: '/api/ml/compare-async', list: '/api/ml/comparisons', detail: '/api/ml/compare/', key: 'comparisons'},
  almd: {title: '多描述符主动学习', start: '/api/al/multidim-async', list: '/api/al/multidim', detail: '/api/al/multidim/', key: 'runs'}
};
function researchField(id, label, value, hint = '') {
  return '<div class="field"><label><b>' + esc(label) + '</b></label><input id="rp-' + id + '" value="' + esc(value) + '"><div class="hint">' + esc(hint) + '</div></div>';
}
async function openResearchPanel(kind) {
  const spec = RESEARCH[kind]; if (!spec) return;
  const options = await loadDatasetOptions('ml');
  let html = '<div class="field"><label><b>训练数据集（必选）</b></label><select id="rp-dataset"><option value="">请选择已导入的 ML 数据集</option>' + options + '</select></div>' +
    '<div class="grid g2">' + researchField('target', '目标列', 'target') +
    researchField('group', '分组列（可选）', '', '如 parent_id；用于检验对新材料组的泛化') + '</div>' +
    researchField('exclude', '排除列（逗号分隔）', 'seed,composition', '排除批次信息及由目标直接推导的特征，避免标签泄漏');
  if (kind === 'compare') {
    html += researchField('models', '对比模型（逗号分隔）', 'ridge,rf,extra_trees', '支持 all；默认小模型组合，使用 CPU / 4 线程') +
      '<div class="grid g2">' + researchField('folds', '交叉验证折数', '5') + researchField('repeats', '重复次数', '10') + '</div>' +
      researchField('split', '预注册切分列（可选）', '', '该列取值 train / validation / test；避免反复选择测试集') +
      '<div class="hint">结果显示打乱重复交叉验证、分组留出与均值基线；默认超参数排名不代表调优后的最终排名。</div>';
  } else {
    html += '<div class="grid g2"><div class="field"><label><b>采集方法</b></label><select id="rp-method"><option value="gpr_ucb">GPR · UCB</option><option value="gpr_ei">GPR · EI</option><option value="gpr_pi">GPR · PI</option></select></div>' +
      researchField('batch', '推荐数量', '10') + '</div>' +
      researchField('candidate', '候选 CSV 文件名（可选）', '', '文件应已在服务器数据集目录中，列名须与训练特征一致；留空只做诊断/模拟') +
      '<label><input id="rp-minimize" type="checkbox" checked> 目标越小越好</label> ' +
      '<label><input id="rp-simulate" type="checkbox"> 运行分组回放模拟（需要分组列）</label>' +
      '<div class="hint">实验性功能：现有分组诊断/回放含全量预处理及噪声标定，不能作为独立泛化验收。推荐不自动提交 CP2K。</div>';
  }
  openModal(spec.title, html, '<button class="btn ghost" onclick="openProcessMonitor()">进程资源</button><button class="btn" onclick="closeModal()">取消</button><button id="rp-start" class="btn primary" onclick="submitResearch(\'' + kind + '\')">启动后台任务</button>');
  if (kind === 'compare') { $('#rp-target').readOnly = true; $('#rp-target').title = 'ML 导入后统一使用 target 列'; }
}
function researchConfig(kind) {
  const value = id => ($('#rp-' + id)?.value || '').trim();
  if (!value('dataset')) throw new Error('请选择独立数据集，避免混入历史样本');
  const cfg = {dataset_file: value('dataset'), dataset_module: 'ml', target: value('target') || 'target',
    group_col: value('group') || undefined, exclude_cols: value('exclude').split(/[,，]/).map(s => s.trim()).filter(Boolean),
    device: 'cpu', n_jobs: 4, preprocess: true};
  const integer = (id, lo, hi) => { const v = Number(value(id)); if (!Number.isInteger(v) || v < lo || v > hi) throw new Error('参数范围：' + id + ' = ' + lo + '–' + hi); return v; };
  if (kind === 'compare') Object.assign(cfg, {models: value('models') || 'all', cv_folds: integer('folds', 2, 10),
    cv_repeats: integer('repeats', 1, 20), fixed_split_col: value('split') || undefined, shap: true});
  else {
    const simulate = $('#rp-simulate').checked;
    if (simulate && !cfg.group_col) throw new Error('分组回放模拟需要明确的分组列');
    Object.assign(cfg, {method: value('method'), batch_size: integer('batch', 1, 100), minimize: $('#rp-minimize').checked,
      simulate, candidate_file: value('candidate') || undefined, sim_rounds: 6, sim_random_repeats: 10});
  }
  return cfg;
}
async function submitResearch(kind) {
  const spec = RESEARCH[kind]; if (!spec) return;
  let body; try { body = researchConfig(kind); } catch (e) { toast(e.message, 'err'); return; }
  const button = $('#rp-start'); button.disabled = true;
  try {
    const r = await api(spec.start, {method: 'POST', body});
    if (!r.ok) { toast(r.error || '启动失败', 'err'); return; }
    toast('已创建后台任务 #' + r.data.task_id); await openResearchRuns(kind);
  } finally { if (button.isConnected) button.disabled = false; }
}
async function openResearchRuns(kind) {
  const spec = RESEARCH[kind]; if (!spec) return;
  const r = await api(spec.list); if (!r.ok) { toast(r.error || '读取失败', 'err'); return; }
  const rows = r.data[spec.key] || [];
  const html = '<table class="tbl"><thead><tr><th>任务</th><th>数据集</th><th>状态</th><th>进度</th><th></th></tr></thead><tbody>' + rows.map(t =>
    '<tr><td>' + esc(t.name || '#' + t.id) + '</td><td>' + esc(t.dataset || '—') + '</td><td>' + esc(t.status) + '</td><td>' + esc(t.progress || 0) + '%</td><td><button class="btn sm" onclick="showResearchResult(\'' + kind + '\',' + Number(t.id) + ')">详情</button></td></tr>').join('') +
    '</tbody></table>' + (!rows.length ? '<p class="hint">暂无任务。创建后可在此刷新进度与结果。</p>' : '');
  openModal(spec.title + ' · 任务与结果', html, '<button class="btn" onclick="openResearchRuns(\'' + kind + '\')">刷新</button><button class="btn" onclick="openResearchPanel(\'' + kind + '\')">新建任务</button><button class="btn" onclick="closeModal()">关闭</button>');
}
let lastResearchResult = null;
async function showResearchResult(kind, id) {
  const spec = RESEARCH[kind]; if (!spec || !Number.isInteger(id)) return;
  const r = await api(spec.detail + id); if (!r.ok) { toast(r.error || '读取失败', 'err'); return; }
  const d = r.data; lastResearchResult = {kind, id, data: d};
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(4) : '—';
  let html = '<p>状态：' + esc(d.status) + ' · 样本 ' + esc(d.n_samples ?? '—') + ' · 特征 ' + esc(d.n_features ?? '—') + '</p>';
  if (d.error) html += '<p class="hint warn">' + esc(d.error) + '</p>';
  if (kind === 'compare') html += '<table class="tbl"><thead><tr><th>模型</th><th>CV R² ± 标准差</th><th>CV MAE（目标单位）</th></tr></thead><tbody>' +
    (d.ranking || []).map(t => '<tr><td>' + esc(t.model) + '</td><td>' + num(t.cv_r2) + ' ± ' + num(t.cv_r2_std) + '</td><td>' + num(t.cv_mae) + '</td></tr>').join('') + '</tbody></table>' +
    '<p class="hint">默认参数下的对比；协议、基线、按组留出结果见下方详情。不同数据集的分数不能直接排名。</p>';
  else html += '<p>候选总数：' + esc(d.n_candidates ?? 0) + ' · 输出推荐：' + (d.recommendations || []).length + '</p><p class="hint">未提供候选文件时不产生待计算结构；模拟效果不等于新体系验证。</p>';
  html += '<details open><summary>协议、诊断与完整接口结果</summary><pre style="white-space:pre-wrap;max-height:45vh;overflow:auto">' + esc(JSON.stringify(d, null, 2)) + '</pre></details>';
  openModal(spec.title + ' #' + id, html, '<button class="btn" onclick="showResearchResult(\'' + kind + '\',' + id + ')">刷新</button><button class="btn" onclick="downloadResearchResult()">导出 JSON</button><button class="btn" onclick="openResearchRuns(\'' + kind + '\')">返回列表</button>');
}
function downloadResearchResult() {
  if (!lastResearchResult) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(lastResearchResult.data, null, 2)], {type: 'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = 'CataMind-' + lastResearchResult.kind + '-' + lastResearchResult.id + '.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
