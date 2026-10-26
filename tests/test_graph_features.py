"""CLI regressions for the evaluation's search, analysis and delivery failures."""
import json
import subprocess
import sys
import unittest
import test_graph


class FeatureTests(unittest.TestCase):
    setUp = test_graph.GraphTests.setUp
    write = test_graph.GraphTests.write
    cli = test_graph.GraphTests.cli
    fixture = test_graph.GraphTests.fixture
    def raw(self, *args):
        result = subprocess.run([sys.executable, str(test_graph.ROOT/'scripts/remember.py'),
                                 *args, '--repo', str(self.repo), '--cache', str(self.cache)],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout

    def test_unknown_mentions_and_paths(self):
        self.write('core.py', 'def save(x):\n    return x\n')
        self.write('caller.py', 'def run(obj):\n    return obj.save(1)\n')
        result = self.cli('neighbors', 'core.py::save', '--grain', 'symbol')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['results'], [])
        gap = result['scope']['unresolved_mentions']
        self.assertEqual(gap['examples'], [{'path': 'caller.py', 'line': 2}])
        result = self.cli('paths', 'caller.py', 'core.py::save')
        self.assertEqual(result['scope']['path_result'], 'no_path_gaps_present')
        impact = self.cli('impact', 'core.py::save')
        full = self.store.load(impact['report'])
        self.assertIn('unresolved_mentions', {g['reason'] for g in full['gaps']})
        self.assertEqual(impact['status'], 'partial')
        table = self.raw('neighbors', 'core.py::save', '--grain', 'symbol', '--format', 'table')
        self.assertIn('unknown', table)
        self.assertIn('caller.py', table)

    def test_resolved_call_is_not_unknown_and_via_is_visible(self):
        self.fixture()
        result = self.cli('neighbors', 'core.py::save', '--grain', 'symbol')
        self.assertNotIn('unresolved_mentions', result['scope'])
        self.assertTrue(all(r['via'] == 'CALLS_STATIC' for r in result['results']))
        self.write('isolated.py', 'def alone():\n    pass\n')
        result = self.cli('paths', 'isolated.py', 'core.py')
        self.assertEqual(result['scope']['path_result'], 'no_supported_path_within_scope')

    def test_find_scope_regex_and_counts(self):
        self.write('src/a.py', 'retry = 1\nretry_again = 2\n')
        self.write('vendor/a.py', 'retry = 1\n')
        result = self.cli('find', '--regex', 'retry.*=', '--path', '*.py', '--not-path', 'vendor', '--kind', 'occurrence', '--count-by', 'path')
        self.assertEqual(result['results'], [{'path': 'src/a.py', 'count': 2, 'kinds': ['occurrence'], 'first_line': 1}])
        self.assertEqual(self.cli('find', 'retry', '--path', 'missing')['results'], [])
        self.cli('find', 'retry', '--regex', 'other', success=False)
        self.cli('find', success=False)
        self.cli('find', '--regex', '(', success=False)

    def test_query_limit_retention_and_truncation(self):
        sql = 'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1200) SELECT x FROM n'
        result = self.cli('query', '--sql', sql)
        self.assertEqual(len(result['results']), 200)
        self.assertIn('query retention capped', result['results_truncated'])
        result = self.cli('query', '--sql', sql, '--limit', '1200', '--max-bytes', '50000')
        self.assertEqual(len(result['results']), 1200)
        self.assertTrue(result['scope']['query_complete'])
        self.assertNotIn('results_truncated', result)
        result = self.cli('query', '--sql', sql, '--offset', '1100', '--limit', '100')
        self.assertEqual(result['results'][0], {'x': 1101})
        self.assertEqual(len(result['results']), 100)

    def test_projection_preserves_retained_report(self):
        self.fixture()
        out = self.cli('neighbors', 'core.py', '--fields', 'path,via', '--limit', '1')
        self.assertEqual(set(out['results'][0]), {'path', 'via'})
        self.assertIn('witness', self.store.load(out['report'])['results'][0])
        self.assertIn('results_truncated', out)
        table = self.raw('neighbors', 'core.py', '--fields', 'path,via', '--format', 'table')
        self.assertIn('CALLS_STATIC', table)
        self.assertIn(out['report'].split(':')[0], table)
        self.cli('map', '--fields', ',', success=False)

    def test_large_map_summary_and_explicit_formats(self):
        self.write('src/a.py', '# source\n' * 3001)
        text = self.raw('map')
        self.assertIn('DIRECTOR', text)
        self.assertIn('3001', text)
        self.assertIn('src/a.py', text)
        self.assertIn('snapshot:', text)
        self.assertIn('results', json.loads(self.raw('map', '--format', 'json')))
        self.assertIn('path', self.raw('map', '--format', 'table'))
        self.assertNotIn('WHOLE REPOSITORY', self.raw('map', '--fields', 'path'))

    def test_analysis_reports_and_filters(self):
        self.fixture()
        self.write('main.py', 'import api\n')
        self.write('cycle_a.py', 'import cycle_b\n')
        self.write('cycle_b.py', 'import cycle_a\n')
        self.write('private.py', 'def _private():\n    pass\n')
        fanin = self.cli('analyze', 'fanin', '--max-bytes', '50000')['results']
        self.assertEqual(next(r for r in fanin if r['path'] == 'core.py')['consumers'], 1)
        cycles = self.cli('analyze', 'cycles')['results']
        self.assertEqual(cycles[0]['files'], ['cycle_a.py', 'cycle_b.py'])
        self.assertEqual(cycles[0]['edge_count'], 2)
        self.assertTrue(all(e['edge'].startswith('snapshot:') for e in cycles[0]['edges']))
        orphans = self.cli('analyze', 'orphans', '--exclude-entrypoints')['results']
        self.assertNotIn('main.py', {r['path'] for r in orphans})
        private = self.cli('analyze', 'orphans', '--grain', 'symbol', '--exclude-public')['results']
        self.assertEqual({r['name'] for r in private}, {'_private'})
        untested = self.cli('analyze', 'untested')['results']
        self.assertNotIn('service.py', {r['path'] for r in untested})
        self.assertEqual(next(r for r in untested if r['path'] == 'core.py')['test_distance'], 2)
        largest = self.cli('analyze', 'largest')['results']
        self.assertTrue(all(r['kind'] == 'symbol' for r in largest))
        self.assertEqual([r['lines'] for r in largest], sorted((r['lines'] for r in largest), reverse=True))
        self.assertFalse(any(r['is_test'] for r in largest))
        included = self.cli('analyze', 'largest', '--include-tests')['results']
        self.assertTrue(any(r['is_test'] for r in included))
        self.cli('analyze', 'cycles', '--grain', 'symbol', success=False)
        self.cli('analyze', 'fanin', '--exclude-public', success=False)


    def test_summary_bounds_and_empty_capture(self):
        self.assertIn('0 readable files', self.raw('map'))
        for i in range(40):
            self.write(f'dir{i}/a.py', '# line\n' * 80)
        text = self.raw('map')
        directory_rows = text.split('DIRECTORIES', 1)[1].split('LARGEST FILES', 1)[0].splitlines()[1:]
        self.assertLessEqual(len([r for r in directory_rows if r.strip()]), 25)
        self.assertIn('(other directories)', text)
        self.assertIn('3200 lines', text)

    def test_history_output_options(self):
        self.fixture()
        result = self.cli('history', 'core.py', '--no-sessions', '--fields', 'kind,path')
        self.assertTrue(all(set(r) <= {'kind', 'path'} for r in result['results']))
        self.assertIn('history', self.raw('history', 'core.py', '--no-sessions', '--format', 'table'))
