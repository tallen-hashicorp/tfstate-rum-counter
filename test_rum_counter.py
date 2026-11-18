"""Synthetic fixtures only; customer state is never needed by the test suite."""
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

import rum_counter as rum


def resource(kind='aws_instance', mode='managed', ids=('one',)):
    return dict(mode=mode, type=kind, name='example', module='module.outer.module.inner',
                instances=[dict(index_key=i, attributes={'id': identifier, 'secret': 'DO_NOT_PRINT'})
                           for i, identifier in enumerate(ids)])


def state(resources=None, **extra):
    return dict(version=4, resources=resources if resources is not None else [resource()], **extra)


class CounterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = rum.main([str(arg) for arg in args])
        return code, out.getvalue()

    def test_count_instances_and_only_official_exclusions(self):
        resources = [resource(ids=('a', 'b', 'c')), resource(mode='data'),
                     resource('null_resource'), resource('terraform_data'),
                     resource('random_id'), resource('local_file'), resource(ids=())]
        resources[0]['instances'][1]['index_key'] = 'DO_NOT_PRINT'
        p = self.write('a.tfstate', state(resources))
        row = rum.scan([p])['files'][0]
        self.assertEqual((row['rum'], row['data_instances'], row['excluded_instances']), (5, 1, 2))

    def test_duplicates_ignore_json_formatting(self):
        a = self.write('a.tfstate', state(lineage='private-lineage'))
        b = self.root / 'b.tfstate'
        b.write_text(json.dumps(state(lineage='private-lineage'), indent=4, sort_keys=True))
        result = rum.scan([a, b])
        self.assertEqual(result['total_rum'], 1)
        self.assertEqual(result['files'][1]['status'], 'duplicate')
        self.assertEqual(result['files'][1]['rum'], 1)

    def test_overlap_and_history_are_flagged_not_subtracted(self):
        a = self.write('a.tfstate', state(lineage='private', serial=1))
        b = self.write('b.tfstate', state(lineage='private', serial=2))
        result = rum.scan([a, b])
        self.assertEqual(result['total_rum'], 2)
        for row in result['files']:
            self.assertTrue(any('Shared lineage' in w for w in row['warnings']))
            self.assertTrue(any('shared resource IDs' in w for w in row['warnings']))
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('DO_NOT_PRINT', json.dumps(result))

    def test_deposed_and_missing_id_warnings(self):
        data = state()
        old = copy.deepcopy(data['resources'][0]['instances'][0])
        old['deposed'] = 'private-deposed-key'
        old['attributes'] = {}
        data['resources'][0]['instances'].append(old)
        row = rum.scan([self.write('a.tfstate', data)])['files'][0]
        self.assertEqual(row['rum'], 2)
        self.assertEqual(row['deposed_instances'], 1)
        self.assertEqual(len(row['warnings']), 2)

    def test_invalid_inputs_skip_whole_file_without_leaking(self):
        cases = [[], {'version': 3}, {'version': 4}, state([{}]),
                 state([resource(mode='unexpected')]), state([dict(mode='managed', type='a', instances=[None])]),
                 {'format_version': '1.0', 'values': {}}]
        for i, case in enumerate(cases):
            self.write(f'{i}.tfstate', case)
        (self.root / 'broken.tfstate').write_text('{"secret":"DO_NOT_PRINT"')
        (self.root / 'duplicate-key.tfstate').write_text('{"version":4,"version":4,"resources":[]}')
        (self.root / 'nan.tfstate').write_text('{"version":4,"resources":[],"secret":NaN}')
        self.write('valid.tfstate', state())
        code, output = self.run_cli(self.root)
        self.assertEqual(code, 1)
        self.assertIn('Total estimated RUM: 1', output)
        self.assertNotIn('DO_NOT_PRINT', output)

    def test_recursive_scan_and_reports(self):
        a = self.write('a.tfstate', state())
        self.write('nested/b.tfstate', state([resource(ids=('two', 'three'))]))
        self.write('nested/b.tfstate.backup', state())
        self.write('.terraform/terraform.tfstate', {'backend': {}})
        self.assertEqual(len(rum.discover(self.root, False)[0]), 1)
        self.assertEqual(len(rum.discover(self.root, True)[0]), 2)
        before = a.read_bytes()
        jp, cp = self.root / 'report.json', self.root / 'report.csv'
        code, output = self.run_cli(self.root, '-r', '--json', jp, '--csv', cp)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(jp.read_text())['total_rum'], 3)
        with cp.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(next(row for row in rows if row['status'] == 'total')['included_rum'], '3')
        self.assertEqual(a.read_bytes(), before)
        self.assertNotIn('DO_NOT_PRINT', jp.read_text() + cp.read_text() + output)

    def test_reports_never_overwrite_or_follow_links(self):
        p = self.write('a.tfstate', state())
        original = p.read_bytes()
        alias = self.root / 'alias.json'
        alias.symlink_to(p)
        for target in (p, alias):
            code, _ = self.run_cli(p, '--json', target)
            self.assertEqual(code, 1)
            self.assertEqual(p.read_bytes(), original)
        existing = self.root / 'existing.csv'
        existing.write_text('keep')
        self.assertEqual(self.run_cli(p, '--csv', existing)[0], 1)
        self.assertEqual(existing.read_text(), 'keep')

    def test_empty_state_empty_scan_and_missing_path(self):
        self.assertEqual(self.run_cli(self.root)[0], 1)
        self.assertEqual(self.run_cli(self.root / 'missing')[0], 1)
        p = self.write('empty.tfstate', state([]))
        self.assertEqual(self.run_cli(p)[0], 0)
        self.assertEqual(rum.scan([p])['total_rum'], 0)

    def test_type_breakdown_and_summary_in_reports(self):
        fixture = state([resource(ids=('a', 'b')), resource('random_id'),
                         resource('terraform_data'), resource(mode='data')])
        self.write('a.tfstate', fixture)
        self.write('duplicate.tfstate', fixture)
        self.write('bad.tfstate', {'version': 3})
        jp, cp = self.root / 'report.json', self.root / 'report.csv'
        code, output = self.run_cli(self.root, '--by-type', '--json', jp, '--csv', cp)
        self.assertEqual(code, 1)
        report = json.loads(jp.read_text())
        self.assertEqual(report['by_type'], {'aws_instance': 2, 'random_id': 1})
        self.assertEqual(sum(report['by_type'].values()), report['total_rum'])
        self.assertEqual(report['summary'], dict(counted=1, duplicate=1, skipped=1,
                                                scan_errors=0, excluded_paths=0))
        self.assertIn('1 counted, 1 duplicates ignored, 1 skipped', output)
        self.assertIn('PARTIAL ESTIMATE', output)
        with cp.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({r['file']: int(r['included_rum']) for r in rows
                          if r['status'] == 'resource_type'}, report['by_type'])
        self.assertNotIn('DO_NOT_PRINT', output + jp.read_text() + cp.read_text())

    def test_exclude_names_paths_and_directories(self):
        self.write('current.tfstate', state())
        self.write('archive/broken.tfstate', {'invalid': True})
        self.write('nested/test-old.tfstate', state())
        self.write('nested/keep.tfstate', state([resource('random_id')]))
        jp = self.root / 'report.json'
        code, output = self.run_cli(self.root, '-r', '--exclude', 'archive',
                                   '--exclude', 'nested/test-*.tfstate', '--json', jp)
        self.assertEqual(code, 0, output)
        report = json.loads(jp.read_text())
        self.assertEqual(report['total_rum'], 2)
        self.assertEqual(report['summary']['excluded_paths'], 2)
        self.assertEqual(report['summary']['skipped'], 0)
        self.assertNotIn('by_type', report)
        self.assertTrue(any(p.endswith('archive/') for p in report['excluded_paths']))
        paths, errors, excluded = rum.discover(self.root, True, ['*.tfstate'])
        self.assertEqual(paths, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(excluded), 4)

    def test_excluded_single_file_and_empty_type_breakdown(self):
        path = self.write('a.tfstate', state())
        code, output = self.run_cli(path, '--exclude', '*.tfstate', '--by-type')
        self.assertEqual(code, 1)
        self.assertIn('0 counted', output)
        self.assertIn('1 paths excluded', output)
        self.assertIn('(no counted resource instances)', output)

    def test_csv_formula_safety(self):
        for text in ('=SUM(1,2)', '+formula', '-formula', '@formula', '\tvalue', '  =formula'):
            self.assertTrue(rum.safe_cell(text).startswith("'"))


if __name__ == '__main__':
    unittest.main()
