"""通知 / 告警文案里的多空方向必须为中文，且五个通道一致。"""
import json
import os
import tempfile
import unittest

from cryptoagent.config import NotifyConfig
from cryptoagent.notify import (
    get_notifier,
    load_notifications,
    localize_direction,
    side_cn,
)


class TestSideCn(unittest.TestCase):
    def test_known_sides(self):
        self.assertEqual(side_cn("LONG"), "多单")
        self.assertEqual(side_cn("long"), "多单")
        self.assertEqual(side_cn("SHORT"), "空单")
        self.assertEqual(side_cn("Short"), "空单")

    def test_unknown_passthrough(self):
        self.assertEqual(side_cn(None), "")
        self.assertEqual(side_cn("FLAT"), "FLAT")


class TestLocalizeDirection(unittest.TestCase):
    def test_replaces_standalone_words(self):
        self.assertEqual(localize_direction("LONG 平仓（模拟盘）"), "多单 平仓（模拟盘）")
        self.assertEqual(localize_direction("short | 止损触发"), "空单 | 止损触发")
        self.assertEqual(localize_direction("做多/做空"), "做多/做空")

    def test_no_false_positive(self):
        # 带下划线的标识符不是方向词，不能被拆开替换
        self.assertEqual(localize_direction("LONG_USDT 已刷新"), "LONG_USDT 已刷新")
        self.assertEqual(localize_direction("决策被风控拦截"), "决策被风控拦截")

    def test_none(self):
        self.assertEqual(localize_direction(None), "")


class TestNotifierDirection(unittest.TestCase):
    def test_inapp_stores_chinese(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notifications.jsonl")
            cfg = NotifyConfig(channels=["inapp"], inapp_path=path)
            get_notifier(cfg).send("info", "LONG 平仓（模拟盘）", "SHORT 方向被拦截")
            rows = load_notifications(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "多单 平仓（模拟盘）")
            self.assertEqual(rows[0]["message"], "空单 方向被拦截")
            # 落盘的就得是中文：webhook / telegram / email 也用同一个 Notification
            with open(path, encoding="utf-8") as fh:
                raw = json.loads(fh.readline())
            self.assertEqual(raw["title"], "多单 平仓（模拟盘）")

    def test_legacy_rows_localized_on_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notifications.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "level": "info", "title": "SHORT 平仓（本地模拟）",
                    "message": "LONG 已补记", "ts": "x", "meta": {},
                }, ensure_ascii=False) + "\n")
            rows = load_notifications(path)
            self.assertEqual(rows[0]["title"], "空单 平仓（本地模拟）")
            self.assertEqual(rows[0]["message"], "多单 已补记")
            # 只读转换，不改写历史文件
            with open(path, encoding="utf-8") as fh:
                self.assertIn("SHORT", fh.read())

    def test_meta_keeps_raw_enum(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notifications.jsonl")
            cfg = NotifyConfig(channels=["inapp"], inapp_path=path)
            get_notifier(cfg).send("info", "LONG 平仓", "x", {"action": "short"})
            rows = load_notifications(path)
            # 结构化字段供 webhook 消费方使用，保持英文枚举不变
            self.assertEqual(rows[0]["meta"]["action"], "short")


if __name__ == "__main__":
    unittest.main()
