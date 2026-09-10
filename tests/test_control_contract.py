"""Both repositories run the same control-v1 fixtures with native validators."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1] / 'contracts' / 'control' / 'v1'
spec = importlib.util.spec_from_file_location('control_contract_reference', ROOT / 'validate.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class ControlContractTests(unittest.TestCase):
    def test_shared_fixtures(self):
        examples = json.loads((ROOT / 'examples.json').read_text())
        fixtures = json.loads((ROOT / 'fixtures.json').read_text())
        self.assertEqual(len({c['name'] for c in fixtures}), len(fixtures))
        for case in fixtures:
            with self.subTest(case=case['name']):
                document = deepcopy(examples[case['base']])
                for edit in case.get('edits', []):
                    target = document
                    for key in edit['path'][:-1]:
                        target = target[key]
                    key = edit['path'][-1]
                    if edit.get('remove'):
                        del target[key]
                    else:
                        target[key] = deepcopy(edit['value'])
                context = examples.get(case.get('definition'))
                if case['valid']:
                    contract.validate(document, context)
                else:
                    with self.assertRaisesRegex(contract.ContractError, '^' + case['error'] + '$'):
                        contract.validate(document, context)

    def test_peer_bundle_matches_when_checked_out(self):
        peer = ROOT.parents[3] / 'smart-home-solutions-t-by' / 'contracts' / 'control' / 'v1'
        if not peer.exists():
            self.skipTest('peer checkout not present; bundle hash remains independently testable')
        self.assertEqual({p.name for p in ROOT.iterdir() if p.is_file()},
                         {p.name for p in peer.iterdir() if p.is_file()})
        for path in ROOT.iterdir():
            if path.is_file():
                self.assertEqual(path.read_bytes(), (peer / path.name).read_bytes(), path.name)
