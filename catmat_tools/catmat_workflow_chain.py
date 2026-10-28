#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CatMat 高通量筛选 AiiDA 工作流（稳定提交方式）。

设计目标（来自平台需求）：
  - 方案 A（默认）：**逐结构链式提交** —— 对每个结构依次执行
        GEO_OPT（结构优化） → ENERGY + PDOS/Bader（单点能及后处理输出）
    一个结构完成后才处理下一个；整体 Wall-clock 更短、故障隔离、数据局部性好。
  - 方案 B（可选）：**波前并行** —— 先统一提交所有结构的 GEO_OPT，
    待全部几何优化完成后再统一提交所有结构的单点能。其独特优势在于：
    *几何预筛选* —— 在「全部优化完成」与「全部单点能」之间可插入过滤/
    筛选步骤（例如只保留能量合理的候选构型再跑昂贵的 PDOS/Bader），
    从而节省后续昂贵计算量。

实现严格参照已验证的 irru_adsorption_workchain.py 模式：
  - 使用 aiida-cp2k 提供的 Cp2kBaseWorkChain（WorkflowFactory("cp2k.base")），
    而非裸 Cp2kCalculation，以获得稳健的重启/容错 handler（如
    restart_incomplete_calculation），这是 AiiDA 推荐的「稳定」提交方式。
  - 用 WorkChain + while_ 组织逐结构循环；Cp2kBaseWorkChain 作为子工作链。
  - 参数集（两套相互独立：GEO_OPT / ENERGY+PDOS）由平台唯一基线模块
    cp2k_workchain_input.py 构建，保证「计算级别参数固化、单点能才追加
    PDOS/Bader」的铁律不被破坏。

提交方式（稳定）：由 submit_catmat_workflow.py 经
    load_profile() → WorkflowFactory(entry_point) → submit(builder)
提交；entry_point 通过 catmat_tools/setup.py 的 aiida.workflows 注册。
daemon 端必须能 import 本模块及其依赖（cp2k_workchain_input），故需在
项目根目录的 aiida 环境中执行 `pip install -e ./catmat_tools --no-deps`，
用 `verdi plugin list aiida.workflows` 验证发现结果，并在维护窗口重启 daemon。
"""
from aiida.engine import WorkChain, while_
from aiida.orm import Code, Dict, Int, List, Str, StructureData, load_node
from aiida.plugins import WorkflowFactory

# 由 aiida-cp2k 提供的 CP2K 计算封装工作链：内含稳健 handler，推荐使用。
Cp2kBaseWorkChain = WorkflowFactory("cp2k.base")

# 平台唯一基线：构建 GEO_OPT / ENERGY+PDOS 两套相互独立的参数集。
# 注意：daemon 运行时需能 import 本模块（经 pip install -e ./catmat_tools 将项目根目录加入路径）。
from catmat_tools.cp2k_workchain_input import (
    build_geo_opt_params, build_single_point_params, build_retrieve_settings,
)


def _extract_energy(sp_node):
    """从 Cp2kBaseWorkChain 的单点能结果中提取总能量（Hartree）。"""
    if not hasattr(sp_node, "outputs"):
        return None
    if hasattr(sp_node.outputs, "output_parameters"):
        try:
            params = sp_node.outputs.output_parameters.get_dict()
            if "energy" in params:
                return params["energy"]
        except Exception:
            pass
    for attr in ("energy", "output_energy"):
        if hasattr(sp_node.outputs, attr):
            try:
                return getattr(sp_node.outputs, attr).value
            except Exception:
                pass
    return None


def _optimized_structure(opt_node):
    """从 GEO_OPT 子链输出中取得优化后的结构（StructureData）。"""
    if not hasattr(opt_node, "outputs"):
        return None
    for attr in ("output_structure", "structure"):
        if hasattr(opt_node.outputs, attr):
            return getattr(opt_node.outputs, attr)
    return None


def _default_options():
    return {
        "resources": {"num_machines": 1, "num_mpiprocs_per_machine": 144},
        "max_wallclock_seconds": 12 * 3600,
    }


# ===========================================================================
# 方案 A：逐结构链式（默认）
# ===========================================================================
class CatMatWorkflowChain(WorkChain):
    """对每个结构串行执行 GEO_OPT -> ENERGY(+PDOS/Bader)。

    输入：
      cp2k_code      (Code, required)    CP2K code（如 cp2k@local）
      structure_pks  (List, required)    已入库 StructureData 的 PK 列表
      labels         (List, required)    与 structure_pks 一一对应的标签
      options        (Dict, optional)    资源设置 {"resources":{...}}
      periodic       (Str,  default XY)  周期边界（传给参数构建器）
      scf_method     (Str,  default diag) diag / ot
      sp_requests    (List, default [pdos,bader,d_band]) 单点能输出请求
      geo_wallclock  (Int,  default 3h)  GEO_OPT 单步 Wall-clock 上限（秒）
      sp_wallclock   (Int,  default 6h)  单点能单步 Wall-clock 上限（秒）
      kind_plans     (Dict, optional)    {label: [kind名,...]} 位点级 KIND 拆分方案；
                                         缺省或某 label 未给出时退化为元素级 KIND
      cube_stride    (Int,  default 1)   &E_DENSITY_CUBE 的 STRIDE，控制 cube 体积
    输出：
      results        (List)  每个结构的 {label, optimization_pk, single_point_pk,
                                 energy, output_structure_pk}
    """

    @classmethod
    def define(cls, spec):
        super().define(spec)
        spec.input("cp2k_code", valid_type=Code, required=True)
        spec.input("structure_pks", valid_type=List, required=True,
                   help="已入库 StructureData 的 PK 列表")
        spec.input("labels", valid_type=List, required=True,
                   help="与 structure_pks 一一对应的编号标签")
        spec.input("options", valid_type=Dict, required=False,
                   help='资源设置，如 {"resources":{"num_machines":1,"num_mpiprocs_per_machine":144}}')
        spec.input("periodic", valid_type=Str, required=False, default=lambda: Str("XY"))
        spec.input("scf_method", valid_type=Str, required=False, default=lambda: Str("diag"))
        spec.input("sp_requests", valid_type=List, required=False,
                   default=lambda: List(list=["pdos", "bader", "d_band"]))
        spec.input("geo_wallclock", valid_type=Int, required=False, default=lambda: Int(3 * 3600))
        spec.input("sp_wallclock", valid_type=Int, required=False, default=lambda: Int(6 * 3600))
        spec.input("kind_plans", valid_type=Dict, required=False,
                   help="{label: [kind名,...]} 位点级 KIND 拆分；缺省则元素级")
        spec.input("cube_stride", valid_type=Int, required=False, default=lambda: Int(1),
                   help="&E_DENSITY_CUBE STRIDE（1=全网格；2/3 大幅省磁盘）")
        spec.output("results", valid_type=List, required=False)
        spec.outline(
            cls.setup,
            while_(cls.should_process_next)(
                cls.load_structure,
                cls.run_optimization,
                cls.run_single_point,
                cls.collect_results,
            ),
            cls.finalize,
        )

    def setup(self):
        self.ctx.structure_pks = list(self.inputs.structure_pks.get_list())
        self.ctx.labels = list(self.inputs.labels.get_list())
        if len(self.ctx.structure_pks) != len(self.ctx.labels):
            self.report("structure_pks 与 labels 长度不一致，按较短截断")
            n = min(len(self.ctx.structure_pks), len(self.ctx.labels))
            self.ctx.structure_pks = self.ctx.structure_pks[:n]
            self.ctx.labels = self.ctx.labels[:n]
        self.ctx.current_index = 0
        self.ctx.results = []
        self.ctx.total = len(self.ctx.structure_pks)
        self.ctx.cp2k_code = self.inputs.cp2k_code
        self.report("准备逐结构链式处理 %d 个结构（方案 A）" % self.ctx.total)

    def should_process_next(self):
        return self.ctx.current_index < self.ctx.total

    def load_structure(self):
        pk = self.ctx.structure_pks[self.ctx.current_index]
        label = self.ctx.labels[self.ctx.current_index]
        try:
            node = load_node(int(pk))
        except Exception as e:
            self.report("PK=%s 加载失败: %s，跳过" % (pk, e))
            self.ctx.current_index += 1
            return
        if not isinstance(node, StructureData):
            self.report("PK=%s 不是 StructureData，跳过" % pk)
            self.ctx.current_index += 1
            return
        self.ctx.current_structure = node
        self.ctx.current_label = label
        self.ctx.current_elements = sorted(node.get_symbols_set())
        self.report("加载结构 %s (PK=%s)" % (label, pk))

    def run_optimization(self):
        if "current_structure" not in self.ctx:
            return
        label = self.ctx.current_label
        self.report("开始结构优化: %s" % label)
        try:
            params = Dict(build_geo_opt_params(
                self.ctx.current_elements,
                periodic=self.inputs.periodic.value,
                scf_method=self.inputs.scf_method.value))
        except Exception as e:
            self.report("构建 GEO_OPT 参数失败: %s，跳过该结构" % e)
            self.ctx.current_index += 1
            return
        builder = Cp2kBaseWorkChain.get_builder()
        builder.cp2k.structure = self.ctx.current_structure
        builder.cp2k.parameters = params
        builder.cp2k.code = self.ctx.cp2k_code
        builder.cp2k.metadata.label = "GEO_OPT_%s" % label
        builder.handler_overrides = Dict({
            "restart_incomplete_calculation": {"enabled": True},
        })
        builder.cp2k.metadata.options = self._stage_options("geo")
        future = self.submit(builder)
        self.to_context(optimization=future)

    def run_single_point(self):
        if "current_structure" not in self.ctx or "optimization" not in self.ctx:
            return
        opt_workchain = self.ctx.optimization
        if not opt_workchain.is_finished_ok:
            self.report("优化工作链失败（exit %s），跳过单点能" % opt_workchain.exit_status)
            self.ctx.current_index += 1
            return
        optimized_structure = _optimized_structure(opt_workchain)
        if optimized_structure is None:
            self.report("无法获取优化后结构，跳过单点能")
            self.ctx.current_index += 1
            return
        label = self.ctx.current_label
        self.report("开始单点能+PDOS: %s" % label)
        try:
            reqs = list(self.inputs.sp_requests.get_list())
            kind_plan = self._kind_plan_for(label)
            params = Dict(build_single_point_params(
                self.ctx.current_elements,
                periodic=self.inputs.periodic.value,
                scf_method=self.inputs.scf_method.value,
                requests=reqs,
                kind_plan=kind_plan,
                cube_stride=self.inputs.cube_stride.value))
        except Exception as e:
            self.report("构建单点能参数失败: %s，跳过" % e)
            self.ctx.current_index += 1
            return
        builder = Cp2kBaseWorkChain.get_builder()
        builder.cp2k.structure = optimized_structure
        builder.cp2k.parameters = params
        builder.cp2k.code = self.ctx.cp2k_code
        builder.cp2k.settings = Dict(build_retrieve_settings(reqs))
        builder.cp2k.metadata.label = "SP_PDOS_%s" % label
        builder.handler_overrides = Dict({
            "restart_incomplete_calculation": {"enabled": True},
        })
        builder.cp2k.metadata.options = self._stage_options("sp")
        future = self.submit(builder)
        self.to_context(single_point=future)

    def collect_results(self):
        if "single_point" not in self.ctx:
            self.report("跳过结果收集（单点计算未执行）")
            self.ctx.current_index += 1
            return
        sp = self.ctx.single_point
        label = self.ctx.current_label
        if not sp.is_finished_ok:
            self.report("单点计算失败（exit %s），结构 %s 跳过" % (sp.exit_status, label))
            self.ctx.current_index += 1
            del self.ctx.single_point
            return
        energy = _extract_energy(sp)
        if energy is None:
            self.report("未能提取能量，结构 %s 跳过" % label)
        else:
            opt_pk = self.ctx.optimization.pk
            out_struct = _optimized_structure(self.ctx.optimization)
            result = {
                "label": label,
                "optimization_pk": opt_pk,
                "single_point_pk": sp.pk,
                "energy": energy,
                "output_structure_pk": out_struct.pk if out_struct is not None else None,
            }
            self.ctx.results.append(result)
            self.report("完成 %s: 能量 = %s Hartree" % (label, energy))
        self.ctx.current_index += 1
        del self.ctx.single_point

    def finalize(self):
        results_node = List(list=self.ctx.results)
        results_node.store()
        # results 在 define 中声明为 required=False，始终可输出（含空列表）
        self.out("results", results_node)
        self.report("方案 A：%d/%d 个结构处理完毕" % (len(self.ctx.results), self.ctx.total))

    # ---- 内部工具 ----
    def _kind_plan_for(self, label):
        """取该 label 的位点级 KIND 方案；未提供则返回 None（元素级）。"""
        if "kind_plans" not in self.inputs:
            return None
        plans = self.inputs.kind_plans.get_dict()
        plan = plans.get(label)
        if plan is None:
            return None
        if not isinstance(plan, (list, tuple)):
            self.report("kind_plans[%s] 不是列表，忽略并退化元素级" % label)
            return None
        return [str(x) for x in plan]

    def _stage_options(self, stage):
        base = dict(self.inputs.options.get_dict()) if "options" in self.inputs else _default_options()
        resources = base.get("resources", {"num_machines": 1, "num_mpiprocs_per_machine": 144})
        wall = int(self.inputs.geo_wallclock) if stage == "geo" else int(self.inputs.sp_wallclock)
        return {
            "resources": dict(resources),
            "max_wallclock_seconds": wall,
        }


# ===========================================================================
# 方案 B：波前并行（先全部 GEO_OPT，再全部单点能）
# ===========================================================================
class CatMatBatchWorkflowChain(WorkChain):
    """波前并行：先统一提交所有结构的 GEO_OPT，全部完成后统一提交单点能。

    独特优势：**几何预筛选** —— 在「全部优化完成」与「全部单点能」之间可
    插入筛选（本实现默认全跑；如需预筛选，可在 wait_optimizations 步骤扩展
    过滤逻辑，仅对通过筛选的构型提交昂贵的 PDOS/Bader 单点能）。

    输入/输出同 CatMatWorkflowChain。
    """

    @classmethod
    def define(cls, spec):
        super().define(spec)
        spec.input("cp2k_code", valid_type=Code, required=True)
        spec.input("structure_pks", valid_type=List, required=True,
                   help="已入库 StructureData 的 PK 列表")
        spec.input("labels", valid_type=List, required=True)
        spec.input("options", valid_type=Dict, required=False)
        spec.input("periodic", valid_type=Str, required=False, default=lambda: Str("XY"))
        spec.input("scf_method", valid_type=Str, required=False, default=lambda: Str("diag"))
        spec.input("sp_requests", valid_type=List, required=False,
                   default=lambda: List(list=["pdos", "bader", "d_band"]))
        spec.input("geo_wallclock", valid_type=Int, required=False, default=lambda: Int(3 * 3600))
        spec.input("sp_wallclock", valid_type=Int, required=False, default=lambda: Int(6 * 3600))
        spec.input("kind_plans", valid_type=Dict, required=False,
                   help="{label: [kind名,...]} 位点级 KIND 拆分；缺省则元素级")
        spec.input("cube_stride", valid_type=Int, required=False, default=lambda: Int(1),
                   help="&E_DENSITY_CUBE STRIDE（1=全网格；2/3 大幅省磁盘）")
        spec.output("results", valid_type=List, required=False)
        spec.outline(
            cls.setup,
            cls.submit_all_optimizations,
            cls.wait_optimizations,
            cls.submit_all_single_points,
            cls.wait_single_points,
            cls.collect_all_results,
            cls.finalize,
        )

    def setup(self):
        self.ctx.structure_pks = list(self.inputs.structure_pks.get_list())
        self.ctx.labels = list(self.inputs.labels.get_list())
        self.ctx.elements_list = []
        self.ctx.results = []
        self.ctx.total = len(self.ctx.structure_pks)
        self.ctx.cp2k_code = self.inputs.cp2k_code
        self.report("准备波前并行处理 %d 个结构（方案 B）" % self.ctx.total)

    def submit_all_optimizations(self):
        futures = {}
        for i, pk in enumerate(self.ctx.structure_pks):
            label = self.ctx.labels[i] if i < len(self.ctx.labels) else "item%d" % i
            try:
                node = load_node(int(pk))
            except Exception as e:
                self.report("PK=%s 加载失败: %s，跳过" % (pk, e))
                continue
            if not isinstance(node, StructureData):
                self.report("PK=%s 不是 StructureData，跳过" % pk)
                continue
            elements = sorted(node.get_symbols_set())
            self.ctx.elements_list.append(elements)
            try:
                params = Dict(build_geo_opt_params(
                    elements, periodic=self.inputs.periodic.value,
                    scf_method=self.inputs.scf_method.value))
            except Exception as e:
                self.report("构建 GEO_OPT 参数失败(PK=%s): %s，跳过" % (pk, e))
                self.ctx.elements_list.pop()
                continue
            builder = Cp2kBaseWorkChain.get_builder()
            builder.cp2k.structure = node
            builder.cp2k.parameters = params
            builder.cp2k.code = self.ctx.cp2k_code
            builder.cp2k.metadata.label = "GEO_OPT_%s" % label
            builder.handler_overrides = Dict({"restart_incomplete_calculation": {"enabled": True}})
            builder.cp2k.metadata.options = self._stage_options("geo")
            futures["opt_%d" % i] = self.submit(builder)
        if futures:
            self.to_context(**futures)
            self.report("已提交 %d 个 GEO_OPT 子链" % len(futures))

    def wait_optimizations(self):
        # to_context 已等待全部完成；此处可做失败统计/预筛选扩展点。
        ok = 0
        for i in range(self.ctx.total):
            wc = self.ctx.get("opt_%d" % i)
            if wc is not None and wc.is_finished_ok:
                ok += 1
        self.report("GEO_OPT 阶段完成：%d/%d 成功" % (ok, self.ctx.total))

    def submit_all_single_points(self):
        futures = {}
        for i in range(self.ctx.total):
            opt_wc = self.ctx.get("opt_%d" % i)
            label = self.ctx.labels[i] if i < len(self.ctx.labels) else "item%d" % i
            if opt_wc is None or not opt_wc.is_finished_ok:
                self.report("opt_%d 未成功，跳过其单点能" % i)
                continue
            optimized_structure = _optimized_structure(opt_wc)
            if optimized_structure is None:
                self.report("opt_%d 无优化结构，跳过" % i)
                continue
            elements = self.ctx.elements_list[i] if i < len(self.ctx.elements_list) else sorted(optimized_structure.get_symbols_set())
            try:
                reqs = list(self.inputs.sp_requests.get_list())
                params = Dict(build_single_point_params(
                    elements, periodic=self.inputs.periodic.value,
                    scf_method=self.inputs.scf_method.value, requests=reqs,
                    kind_plan=self._kind_plan_for(label),
                    cube_stride=self.inputs.cube_stride.value))
            except Exception as e:
                self.report("构建单点能参数失败(item %d): %s，跳过" % (i, e))
                continue
            builder = Cp2kBaseWorkChain.get_builder()
            builder.cp2k.structure = optimized_structure
            builder.cp2k.parameters = params
            builder.cp2k.code = self.ctx.cp2k_code
            builder.cp2k.settings = Dict(build_retrieve_settings(reqs))
            builder.cp2k.metadata.label = "SP_PDOS_%s" % label
            builder.handler_overrides = Dict({"restart_incomplete_calculation": {"enabled": True}})
            builder.cp2k.metadata.options = self._stage_options("sp")
            futures["sp_%d" % i] = self.submit(builder)
        if futures:
            self.to_context(**futures)
            self.report("已提交 %d 个单点能子链" % len(futures))

    def wait_single_points(self):
        ok = 0
        for i in range(self.ctx.total):
            wc = self.ctx.get("sp_%d" % i)
            if wc is not None and wc.is_finished_ok:
                ok += 1
        self.report("单点能阶段完成：%d/%d 成功" % (ok, self.ctx.total))

    def collect_all_results(self):
        for i in range(self.ctx.total):
            sp = self.ctx.get("sp_%d" % i)
            label = self.ctx.labels[i] if i < len(self.ctx.labels) else "item%d" % i
            if sp is None or not sp.is_finished_ok:
                self.report("item %d 单点能未成功，跳过" % i)
                continue
            energy = _extract_energy(sp)
            if energy is None:
                self.report("item %d 未能提取能量，跳过" % i)
                continue
            out_struct = _optimized_structure(self.ctx.get("opt_%d" % i)) if self.ctx.get("opt_%d" % i) else None
            self.ctx.results.append({
                "label": label,
                "optimization_pk": self.ctx.get("opt_%d" % i).pk if self.ctx.get("opt_%d" % i) else None,
                "single_point_pk": sp.pk,
                "energy": energy,
                "output_structure_pk": out_struct.pk if out_struct is not None else None,
            })
            self.report("完成 %s: 能量 = %s Hartree" % (label, energy))

    def finalize(self):
        results_node = List(list=self.ctx.results)
        results_node.store()
        # results 在 define 中声明为 required=False，始终可输出（含空列表）
        self.out("results", results_node)
        self.report("方案 B：%d/%d 个结构处理完毕" % (len(self.ctx.results), self.ctx.total))

    def _kind_plan_for(self, label):
        """取该 label 的位点级 KIND 方案；未提供则返回 None（元素级）。"""
        if "kind_plans" not in self.inputs:
            return None
        plans = self.inputs.kind_plans.get_dict()
        plan = plans.get(label)
        if plan is None:
            return None
        if not isinstance(plan, (list, tuple)):
            self.report("kind_plans[%s] 不是列表，忽略并退化元素级" % label)
            return None
        return [str(x) for x in plan]

    def _stage_options(self, stage):
        base = dict(self.inputs.options.get_dict()) if "options" in self.inputs else _default_options()
        resources = base.get("resources", {"num_machines": 1, "num_mpiprocs_per_machine": 144})
        wall = int(self.inputs.geo_wallclock) if stage == "geo" else int(self.inputs.sp_wallclock)
        return {
            "resources": dict(resources),
            "max_wallclock_seconds": wall,
        }
