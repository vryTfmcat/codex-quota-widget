from pathlib import Path
import datetime as dt
import tempfile
import unittest

from usage_history import UsageHistory, intervals


def sample(at, five=10, week=40, five_reset=2000000000, week_reset=2000500000):
    return {'at': at, 'credits': 3, 'windows': [
        {'windowDurationMins': 300, 'usedPercent': five, 'resetsAt': five_reset},
        {'windowDurationMins': 10080, 'usedPercent': week, 'resetsAt': week_reset}]}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.h = UsageHistory(self.root / 'data.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_intervals_reset_and_gap(self):
        values = [sample(1000), sample(1600, 20, 42), sample(1660, 25, 43),
                  sample(3000, 30, 45), sample(3600, 0, 46, five_reset=2000018000),
                  sample(4200, 5, 10, week_reset=2001500000), sample(4800, 6, 9, week_reset=2001500000)]
        for value in values:
            self.h.record(value)
        self.h.record(values[-1])
        rows = self.h.recent(5000)
        self.assertEqual(len(rows), 7)
        parts = intervals(rows)
        self.assertEqual([p['week'] for p in parts], [2, 1, 2, 1, None, None])
        self.assertTrue(parts[2]['gap'])
        self.assertIsNone(parts[3]['five'])
        self.assertEqual(UsageHistory(self.h.path).baseline(2000500000)['at'], 1000)

    def test_markdown_month_split_and_guards(self):
        vault = self.root / 'vault'
        (vault / '.obsidian').mkdir(parents=True)
        start = dt.datetime(2026, 9, 30, 23, 55).timestamp()
        self.h.record(sample(start))
        self.h.record(sample(start + 600, 20, 42), 'manual')
        result = self.h.export_markdown(vault, 'records')
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['files'], 3)
        self.assertEqual(self.h.export_markdown(vault, 'records')['files'], 0)
        overview = vault / 'records/Codex额度使用记录.md'
        overview.write_text('Keep my own note.', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'not-owned'):
            self.h.export_markdown(vault, 'records')
        self.assertEqual(overview.read_text(encoding='utf-8'), 'Keep my own note.')
        with self.assertRaisesRegex(RuntimeError, 'outside-vault'):
            self.h.export_markdown(vault, '../outside')

    def test_error_text_is_not_stored(self):
        self.h.record_error('a-secret-or-private-url')
        with self.h.connection() as db:
            self.assertEqual(db.execute('SELECT category FROM errors').fetchone()[0], 'unavailable')


if __name__ == '__main__':
    unittest.main()
