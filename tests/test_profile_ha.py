"""Linux process gauges in the external read-only sampler."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('profile_ha', Path(__file__).parents[1] / 'scripts/profile-ha.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProcessMemoryTests(unittest.TestCase):
    def test_current_residency_is_distinct_from_peak_and_units_are_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '71').mkdir()
            (root / '71/status').write_text(
                'Name:\tpython\nVmRSS:\t200 kB\nVmHWM:\t900 kB\n'
                'RssAnon:\t150 kB\nRssFile:\t40 kB\nRssShmem:\t10 kB\n'
                'VmSwap:\t30 kB\nThreads:\t12\n')
            result = profile.process_memory(71, root)
            self.assertEqual(result['rss_bytes'], 200 * 1024)
            self.assertEqual(result['rss_high_water_bytes'], 900 * 1024)
            self.assertEqual(result['swap_bytes'], 30 * 1024)
            self.assertEqual(result['threads'], 12)
