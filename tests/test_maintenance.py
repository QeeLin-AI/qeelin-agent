"""长期运行的运维细节：账本每日备份、决策快照清理、配置原子写。

这些不是业务逻辑，但都是「跑上几个月才炸」的问题：
- 账本只有一份，写坏没得回退；
- 决策快照 5 分钟一份，不清就一年十万个文件；
- config 直接 write_text，中途失败会留下半截 JSON，下次启动整站起不来。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _engine(tmp: str, keep_days: int = 7):
    from cryptoagent.engine import TradingEngine

    e = object.__new__(TradingEngine)
    e.lock = threading.RLock()
    e.state_path = Path(tmp) / "engine_state.json"
    e.state = {"mode": "paper", "equity": 10_000.0}
    e.cfg = types.SimpleNamespace(decision_keep_days=keep_days)
    return e


class TestStateBackup(unittest.TestCase):
    def test_save_creates_one_copy_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = _engine(tmp)
            e._save()
            day = time.strftime("%Y%m%d")
            dst = Path(tmp) / "backup" / f"engine_state-{day}.json"
            self.assertTrue(dst.exists(), "首次保存应留下当天快照")
            self.assertEqual(json.loads(dst.read_text(encoding="utf-8"))["equity"], 10_000.0)

    def test_second_save_same_day_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = _engine(tmp)
            e._save()
            day = time.strftime("%Y%m%d")
            dst = Path(tmp) / "backup" / f"engine_state-{day}.json"
            first = dst.stat().st_mtime_ns
            time.sleep(0.02)
            e.state["equity"] = 12_345.0
            e._save()
            self.assertEqual(dst.stat().st_mtime_ns, first, "同一天不应重复覆盖快照")

    def test_keeps_only_latest_fourteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backup"
            bdir.mkdir()
            for i in range(20):
                (bdir / f"engine_state-202601{i:02d}.json").write_text("{}", encoding="utf-8")
            e = _engine(tmp)
            e._save()
            self.assertEqual(len(list(bdir.glob("engine_state-*.json"))), 14)

    def test_backup_failure_never_breaks_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = _engine(tmp)
            # backup 路径被一个同名文件占住 → mkdir 会失败，但 _save 必须照常成功
            (Path(tmp) / "backup").write_text("not a dir", encoding="utf-8")
            e._save()
            self.assertTrue(e.state_path.exists())
            self.assertEqual(json.loads(e.state_path.read_text(encoding="utf-8"))["equity"], 10_000.0)


class TestDecisionPrune(unittest.TestCase):
    def test_removes_expired_keeps_latest_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            old = out / "20260101_000000_BTCUSDT.json"
            fresh = out / "20261105_000000_BTCUSDT.json"
            latest = out / "latest.md"
            for f in (old, fresh, latest):
                f.write_text("{}", encoding="utf-8")
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))

            e = _engine(tmp, keep_days=7)
            e._prune_decisions(out)

            self.assertFalse(old.exists(), "超过保留期的快照应删除")
            self.assertTrue(fresh.exists(), "保留期内的快照不能动")
            self.assertTrue(latest.exists(), "latest.md 不是快照，必须保留")

    def test_throttled_within_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            old = out / "20260101_000000_BTCUSDT.json"
            old.write_text("{}", encoding="utf-8")
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))

            e = _engine(tmp, keep_days=7)
            e._prune_decisions(out)          # 第一次清理，删掉
            again = out / "20260102_000000_BTCUSDT.json"
            again.write_text("{}", encoding="utf-8")
            os.utime(again, (stale, stale))
            e._prune_decisions(out)          # 一小时内不再扫盘

            self.assertTrue(again.exists(), "一小时内应跳过清理，避免每轮都遍历目录")

    def test_keep_days_zero_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            fresh = out / "20261105_000000_BTCUSDT.json"
            fresh.write_text("{}", encoding="utf-8")
            e = _engine(tmp, keep_days=0)
            e._prune_decisions(out)
            self.assertTrue(fresh.exists())


class TestSaveConfigAtomic(unittest.TestCase):
    def test_writes_valid_json_without_tmp_leftover(self):
        from cryptoagent.config import AppConfig, save_config

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config" / "default.json"
            save_config(AppConfig(), p)
            self.assertTrue(p.exists())
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("risk", data)
            self.assertEqual(list(p.parent.glob("*.tmp")), [], "临时文件必须已被替换掉")

    def test_decision_keep_days_has_default(self):
        from cryptoagent.config import AppConfig

        self.assertEqual(AppConfig().decision_keep_days, 7)

    def test_old_config_without_new_field_still_loads(self):
        from cryptoagent.config import _build, AppConfig

        cfg = _build(AppConfig, {"runs_dir": "runs", "risk": {"sizing_mode": "risk"}})
        self.assertEqual(cfg.decision_keep_days, 7)
        self.assertEqual(cfg.risk.sizing_mode, "risk")


if __name__ == "__main__":
    unittest.main()
