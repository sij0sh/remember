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
        self.assertFalse(any(r.get('is_test') for r in largest))
        included = self.cli('analyze', 'largest', '--include-tests')['results']
        self.assertTrue(any(r.get('is_test') for r in included))
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

    def test_query_table_keeps_aliases_and_aggregates_in_select_order(self):
        self.fixture()
        sql = "SELECT path, count(*) AS n FROM nodes WHERE path IS NOT NULL GROUP BY path ORDER BY path"
        rows = self.cli('query', '--sql', sql)['results']
        lines = self.raw('query', '--sql', sql, '--format', 'table').splitlines()
        header = next(i for i, line in enumerate(lines) if line.split()[:2] == ['path', 'n'])
        for row, line in zip(rows, lines[header + 1:]):
            self.assertEqual(line.split(), [row['path'], str(row['n'])])
        table = self.raw('query', '--sql', 'SELECT count(*) AS c, min(path) AS p FROM files', '--format', 'table')
        self.assertIn('c  p', table)

    def test_subcommand_errors_name_the_subcommand(self):
        self.fixture()
        result = subprocess.run([sys.executable, str(test_graph.ROOT/'scripts/remember.py'), 'impact', 'core.py',
                                 '--grain', 'symbol', '--repo', str(self.repo), '--cache', str(self.cache)],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn('usage: remember.py impact', result.stderr)
        self.assertIn('impact is file-level', result.stderr)
        alias = self.cli('analyze', 'largest', '--kind', 'symbol')
        self.assertEqual(alias['results'], self.cli('analyze', 'largest', '--grain', 'symbol')['results'])

    def test_history_output_options(self):
        self.fixture()
        result = self.cli('history', 'core.py', '--no-sessions', '--fields', 'kind,path')
        self.assertTrue(all(set(r) <= {'kind', 'path'} for r in result['results']))
        self.assertIn('history', self.raw('history', 'core.py', '--no-sessions', '--format', 'table'))

    def test_generated_code_leaves_rankings_and_is_listed(self):
        self.fixture()
        self.write('vendor/bundle.js', 'export function huge() {\n' + '  let x = 1;\n' * 50 + '}\n')
        self.write('types/host.d.ts', '// Written by a tool; regenerate with /plugin-types\n'
                   'export declare function hostCall(): void;\n')
        largest = self.cli('analyze', 'largest', '--grain', 'file')
        paths = {r['path'] for r in largest['results']}
        self.assertNotIn('vendor/bundle.js', paths)
        self.assertNotIn('types/host.d.ts', paths)
        excluded = largest['scope']['excluded_generated']
        self.assertEqual({f['path']: f['reason'] for f in excluded['files']},
                         {'vendor/bundle.js': 'folder:vendor', 'types/host.d.ts': 'header'})
        included = self.cli('analyze', 'largest', '--grain', 'file', '--include-generated')
        self.assertIn('vendor/bundle.js', {r['path'] for r in included['results']})
        self.assertIn('generated/vendored, left out of analyze rankings: 2 files', self.raw('map', '--no-primer'))

    def test_rust_cfg_test_modules_are_tests(self):
        self.write('Cargo.toml', '[package]\nname = "demo"\n')
        self.write('src/lib.rs', 'pub mod numeric;\npub mod plain;\n')
        self.write('src/numeric.rs', 'pub fn clamp(x: i32) -> i32 { x }\n\n#[cfg(test)]\nmod tests {\n'
                   '    use super::*;\n    #[test]\n    fn clamps() { assert_eq!(clamp(1), 1); }\n}\n')
        self.write('src/plain.rs', 'pub fn bare() {}\n')
        untested = self.cli('analyze', 'untested')
        paths = {r['path'] for r in untested['results']}
        self.assertNotIn('src/numeric.rs', paths)
        self.assertIn('src/plain.rs', paths)
        self.assertEqual(untested['scope']['inline_tested'], ['src/numeric.rs'])
        symbols = self.cli('map', '--kind', 'symbol', '--verbose', '--max-bytes', '40000')['results']
        flags = {r['qualname']: r['is_test'] for r in symbols if r['path'] == 'src/numeric.rs'}
        self.assertEqual(flags, {'clamp': False, 'tests': True, 'tests::clamps': True})

    def test_manifest_references_link_files_and_flag_missing_paths(self):
        self.write('package.json', '{\n  "scripts": {\n    "build": "node scripts/build.mjs --outfile dist/out.js",\n'
                   '    "typecheck": "tsc --noEmit -p worker"\n  },\n  "main": "dist/out.js"\n}\n')
        self.write('tsconfig.json', '{\n  "extends": "./.plugin/types/tsconfig.json",\n  "include": ["src", ".plugin/types"]\n}\n')
        self.write('scripts/build.mjs', 'console.log(1)\n')
        self.write('worker/tsconfig.json', '{}\n')
        self.write('src/index.ts', 'export const a = 1;\n')
        self.write('.github/workflows/ci.yml', 'jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n'
                   '      - run: ./scripts/check.sh && node scripts/build.mjs\n')
        edges = self.cli('query', '--sql', "SELECT s.path AS src, t.path AS dst FROM edges e JOIN nodes s ON s.id = e.source "
                         "JOIN nodes t ON t.id = e.target WHERE e.predicate = 'REFERENCES' ORDER BY 1, 2")['results']
        self.assertEqual({(r['src'], r['dst']) for r in edges},
                         {('package.json', 'scripts/build.mjs'), ('package.json', 'worker'),
                          ('tsconfig.json', 'src'), ('.github/workflows/ci.yml', 'scripts/build.mjs')})
        text = self.raw('map', '--no-primer').split('\n==> ')[0]
        self.assertIn('referenced but missing: .plugin/types/tsconfig.json (named at tsconfig.json:2)', text)
        self.assertIn('referenced but missing: .plugin/types (named at tsconfig.json:3)', text)
        self.assertIn('referenced but missing: scripts/check.sh', text)
        self.assertNotIn('dist/out.js', text)
        orphans = self.cli('analyze', 'orphans')['results']
        build = next(r for r in orphans if r['path'] == 'scripts/build.mjs')
        self.assertEqual(build['entrypoint_hint'], 'manifest_reference')
        self.assertIn('package.json', build['referenced_by'])

    def test_impact_separates_direct_file_only_and_indirect_consumers(self):
        self.write('core.py', 'def save(value):\n    return value\ndef load():\n    return 1\n')
        self.write('saver.py', 'from core import save\ndef run():\n    return save(1)\n')
        self.write('loader.py', 'from core import load\ndef run():\n    return load()\n')
        self.write('api.py', 'from saver import run\ndef go():\n    return run()\n')
        result = self.cli('impact', 'core.py::save', '--max-bytes', '40000')
        reach = {r['path']: r['reach'] for r in result['results'] if r.get('role') == 'possible_consumer'}
        self.assertEqual(reach, {'saver.py': 'direct', 'loader.py': 'file_only', 'api.py': 'indirect'})
        profile = next(r for r in result['results'] if r['kind'] == 'risk_profile')
        self.assertEqual((profile['direct_consumers'], profile['file_only_consumers'], profile['indirect_consumers']),
                         (1, 1, 1))
        whole = self.cli('impact', 'core.py', '--max-bytes', '40000')
        self.assertEqual({r['path']: r['reach'] for r in whole['results'] if r.get('role') == 'possible_consumer'},
                         {'saver.py': 'direct', 'loader.py': 'direct', 'api.py': 'indirect'})

    def test_default_rows_are_compact_and_verbose_keeps_identifiers(self):
        self.fixture()
        row = self.cli('analyze', 'fanin')['results'][0]
        self.assertFalse({'id', 'key', 'digest', 'parse_state'} & set(row))
        self.assertTrue(row['ref'].startswith('snapshot:'))
        full = self.cli('analyze', 'fanin', '--verbose')['results'][0]
        self.assertTrue({'id', 'key', 'digest'} <= set(full))
        step = self.cli('neighbors', 'core.py', '--depth', '3')['results'][0]['witness'][0]
        self.assertEqual(set(step), {'predicate', 'edge'})
        self.assertEqual(self.cli('show', step['edge'])['results'][0]['predicate'], step['predicate'])
