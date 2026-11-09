"""原生 API 编码适配回归；合成工件，无真实工程写入。"""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pcb_geometry import GeometryError
from pcb_native_poured_geometry import validate_via_anchor_calibration
from test_pcb_native_poured_strokes import synthetic_snapshot


class AdapterEncodingTests(unittest.TestCase):
    def calibration(self):
        board, _ = synthetic_snapshot()
        calibration = board['metadata']['native_pour_geometry']['calibration']
        for via in calibration['via_api']:
            via['x'] += .00003
        return calibration

    def test_python_anchor_encoding_preserves_measurements(self):
        c = self.calibration()
        original = deepcopy(c)
        p = validate_via_anchor_calibration(c, 'synthetic-pcb', 10)
        self.assertEqual(p['matched_circle_count'], 8)
        self.assertEqual(len(p['getter_position_quantization_proofs']), 8)
        self.assertEqual(c, original)

    def test_python_anchor_rejects_wrong_evidence(self):
        mutations = [lambda c: c['via_api'][0].update(x=c['via_api'][0]['x']+.01),
                     lambda c: c['via_api'][0].update(hole=7),
                     lambda c: c['via_api'][0].update(net='wrong'),
                     lambda c: c.update(tolerance_mil=.01),
                     lambda c: c.update(source_native=c['source_native'].replace('synthetic-pcb', 'other'))]
        for mutate in mutations:
            c = self.calibration()
            mutate(c)
            with self.assertRaises(GeometryError):
                validate_via_anchor_calibration(c, 'synthetic-pcb', 10)

    def test_adapter_node_regressions(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('需要 Node 执行适配器回归')
        result = subprocess.run([node, '--test', str(Path(__file__).with_suffix('.mjs'))],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
