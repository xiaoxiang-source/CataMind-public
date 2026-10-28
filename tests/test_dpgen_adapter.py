#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""势函数延拓（DPGEN）适配器纯函数单元测试。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "catmat_tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "catmat_tools"))

import dpgen_adapter as A


class TestValidate(unittest.TestCase):
    def test_ok(self):
        ok, errs = A.validate_dpgen_config(
            {"run_name": "t", "seed": "x", "max_iter": 3, "trust_lo": 0.05, "trust_hi": 0.15})
        self.assertTrue(ok, errs)

    def test_missing_name(self):
        ok, errs = A.validate_dpgen_config({"seed": "x", "max_iter": 3})
        self.assertFalse(ok)
        self.assertTrue(any("run_name" in e for e in errs))

    def test_bad_iter(self):
        ok, errs = A.validate_dpgen_config({"run_name": "t", "seed": "x", "max_iter": 99})
        self.assertFalse(ok)

    def test_bad_trust(self):
        ok, _ = A.validate_dpgen_config({"run_name": "t", "seed": "x", "trust_lo": 0.5, "trust_hi": 0.1})
        self.assertFalse(ok)


class TestModelDevi(unittest.TestCase):
    def test_parse_and_screen(self):
        md = "# step a b c\n1 0.01 0.02 0.03\n2 0.10 0.12 0.11\n3 0.50 0.60 0.55\n4 0.08 0.09 0.07\n"
        frames = A.parse_model_devi(md)
        self.assertEqual([f["max_devi"] for f in frames], [0.03, 0.12, 0.6, 0.09])
        cand = A.screen_candidates(frames, 0.05, 0.15)
        # 帧 2 (0.12) 与帧 4 (0.09) 落在 (0.05, 0.15)
        self.assertEqual(cand, [1, 3])


class TestLcurve(unittest.TestCase):
    def test_parse_and_converge(self):
        lc = ("# step rmse_e_train rmse_f_train rmse_val_e rmse_val_f\n"
              "1000 0.080 0.0040 0.085 0.0042\n"
              "2000 0.045 0.0015 0.048 0.0016\n"
              "4000 0.042 0.0014 0.045 0.0015\n")
        hist = A.parse_lcurve(lc)
        self.assertEqual(len(hist), 3)
        self.assertAlmostEqual(hist[-1]["rmse_e_train"], 0.042, 4)
        conv = A.assess_convergence(hist)
        # 误差仍在下降 → 不收敛，给出原因
        self.assertFalse(conv["converged"])
        self.assertTrue(conv["reasons"])

    def test_converged_plateau(self):
        # 三轮平台期且误差达标 → 收敛（需带 lcurve 表头，否则 parse_lcurve 跳过）
        lc = ("# step rmse_e_train rmse_f_train rmse_val_e rmse_val_f\n"
              + "\n".join("%d 0.030 0.0010 0.032 0.0011" % (i * 1000) for i in range(1, 5)))
        hist = A.parse_lcurve(lc)
        conv = A.assess_convergence(hist, {"rmse_e_max": 0.05, "rmse_f_max": 0.05, "min_iters": 2, "patience": 3})
        self.assertTrue(conv["converged"], conv["reasons"])


class TestCp2kFpInp(unittest.TestCase):
    def test_keywords_present(self):
        struct = {"elements": ["Ir", "Ru", "Ir"],
                  "coords": [[0, 0, 0], [1.0, 0, 0], [0, 1.0, 0]],
                  "cell": [[3.0, 0, 0], [0, 3.0, 0], [0, 0, 3.0]], "pbc": True}
        inp = A.build_cp2k_fp_inp(struct)
        for kw in ("RUN_TYPE ENERGY_FORCE", "&FORCES ON", "&STRESS_TENSOR ON"):
            self.assertIn(kw, inp, "缺少关键段: " + kw)
        self.assertIn("Ir", inp)
        self.assertIn("Ru", inp)


class TestDriveIteration(unittest.TestCase):
    def test_full_loop_records_label_calls(self):
        state = {"iter_no": 0, "max_iter": 4, "trust_lo": 0.05, "trust_hi": 0.15,
                 "stride": 1, "lcurve_history": []}
        labels = []

        def ex_train(s):
            return "1000 0.080 0.004\n2000 0.050 0.002\n4000 0.042 0.0014"

        def ex_explore(g, i):
            return "1 0.01 0.02\n2 0.10 0.12\n3 0.08 0.07"

        def ex_label(cands, i):
            labels.append((i, list(cands)))
            return {"queue_ids": [100 + c for c in cands]}

        # 严格判据：能量误差始终超标 → 走满 max_iter，每轮都探索+标注
        crit = {"rmse_e_max": 0.001, "rmse_f_max": 0.001, "min_iters": 2, "patience": 3}
        iters = 0
        while state["iter_no"] < state["max_iter"]:
            state, rec = A.drive_iteration(state,
                {"train": ex_train, "explore": ex_explore, "label": ex_label},
                criteria=crit)
            iters += 1
            if state.get("converged"):
                break
            if rec["phase"] == "done":
                break
        self.assertEqual(iters, 4)
        # 每轮都应有 2 个候选被提交标注
        self.assertEqual(len(labels), 4)
        self.assertEqual(labels[0][1], [1, 2])
        self.assertTrue(all(len(c) == 2 for _, c in labels))


if __name__ == "__main__":
    unittest.main()
