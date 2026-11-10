"""Search, projection, paging and parser edge cases over the graph CLI."""
import json
from graph_fixture import GraphCase
from helper.graph_queries import impact
from support import invoke


class FeatureTests(GraphCase):
    def raw(self, *args):
        result = invoke([*args, '--repo', str(self.repo), '--cache', str(self.cache)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout

    def test_unresolved_mentions_are_a_gap(self):
        self.write('core.py', 'def save(x):\n    return x\n')
        self.write('caller.py', 'def run(obj):\n    return obj.save(1)\n')
        gap = next(g for g in impact(self.graph(), ['core.py::save'])['gaps']
                   if g.get('reason') == 'unresolved_mentions')
        self.assertEqual(gap['examples'], [{'path': 'caller.py', 'line': 2}])

    def test_find_scope_regex_and_counts(self):
        self.write('src/a.py', 'retry = 1\nretry_again = 2\n')
        self.write('vendor/a.py', 'retry = 1\n')
        result = self.cli('find', '--regex', 'retry.*=', '--path', '*.py', '--not-path', 'vendor', '--kind', 'occurrence', '--count-by', 'path')
        self.assertEqual(result['results'], [{'path': 'src/a.py', 'count': 2, 'kinds': ['occurrence'], 'first_line': 1}])
        self.assertEqual(self.cli('find', 'retry', '--path', 'missing')['results'], [])
        self.cli('find', 'retry', '--regex', 'other', success=False)
        self.cli('find', success=False)
        self.cli('find', '--regex', '(', success=False)

    def test_query_limit_and_paging(self):
        sql = 'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1200) SELECT x FROM n'
        result = self.cli('query', sql)
        self.assertEqual(len(result['results']), 200)
        self.assertIn('follow next', result['results_truncated'])
        result = self.cli('query', sql, '--limit', '1200', '--max-bytes', '50000')
        self.assertEqual(len(result['results']), 1200)
        self.assertTrue(result['delivery']['complete'])
        self.assertNotIn('results_truncated', result)
        result = self.cli('query', sql, '--offset', '1100', '--limit', '100')
        self.assertEqual(result['results'][0], {'x': 1101})
        self.assertEqual(len(result['results']), 100)

    def test_projection_selects_columns_and_pages(self):
        self.fixture()
        out = self.cli('map', '--kind', 'file', '--fields', 'path,kind', '--limit', '1')
        self.assertEqual(set(out['results'][0]), {'path', 'kind'})
        self.assertIn('results_truncated', out)
        following = self.cli('show', out['next'][0]['ref'], '--fields', 'path,kind,digest')
        self.assertIn('digest', following['results'][0])
        table = self.raw('map', '--kind', 'file', '--fields', 'path,kind', '--format', 'table')
        self.assertIn('core.py', table)
        self.assertIn('transient', table)
        self.cli('map', '--fields', ',', success=False)

    def test_large_map_summary_and_explicit_formats(self):
        self.write('src/a.py', '# source\n' * 3001)
        text = self.raw('map', '--no-primer')
        self.assertIn('DIRECTORIES', text)
        self.assertIn('3001', text)
        self.assertIn('src/a.py', text)
        self.assertIn('snapshot:', text)
        self.assertIn('results', json.loads(self.raw('map', '--format', 'json', '--no-primer')))
        self.assertIn('path', self.raw('map', '--format', 'table', '--no-primer'))
        self.assertNotIn('WHOLE REPOSITORY', self.raw('map', '--fields', 'path', '--no-primer'))

    def test_summary_bounds_and_empty_capture(self):
        self.assertIn('0 readable files', self.raw('map', '--no-primer'))
        for i in range(40):
            self.write(f'dir{i}/a.py', '# line\n' * 80)
        text = self.raw('map', '--no-primer')
        directory_rows = text.split('DIRECTORIES', 1)[1].split('LARGEST FILES', 1)[0].splitlines()[1:]
        self.assertLessEqual(len([r for r in directory_rows if r.strip()]), 25)
        self.assertIn('(other directories)', text)
        self.assertIn('3200 lines', text)

    def test_query_table_keeps_aliases_and_aggregates_in_select_order(self):
        self.fixture()
        sql = "SELECT path, count(*) AS n FROM nodes WHERE path IS NOT NULL GROUP BY path ORDER BY path"
        rows = self.cli('query', sql)['results']
        lines = self.raw('query', sql, '--format', 'table').splitlines()
        header = next(i for i, line in enumerate(lines) if line.split()[:2] == ['path', 'n'])
        for row, line in zip(rows, lines[header + 1:]):
            self.assertEqual(line.split(), [row['path'], str(row['n'])])
        table = self.raw('query', 'SELECT count(*) AS c, min(path) AS p FROM files', '--format', 'table')
        self.assertIn('c  p', table)

    def test_subcommand_errors_name_the_subcommand(self):
        self.fixture()
        result = invoke(['find', 'save', '--bogus', '--repo', str(self.repo), '--cache', str(self.cache)])
        self.assertEqual(result.returncode, 2)
        self.assertIn('usage: remember.py find', result.stderr)

    def test_generated_code_is_listed(self):
        self.fixture()
        self.write('vendor/bundle.js', 'export function huge() {\n' + '  let x = 1;\n' * 50 + '}\n')
        self.write('types/host.d.ts', '// Written by a tool; regenerate with /plugin-types\n'
                   'export declare function hostCall(): void;\n')
        self.assertIn('generated/vendored: 2 files', self.raw('map', '--no-primer'))

    def test_rust_cfg_test_modules_are_tests(self):
        self.write('Cargo.toml', '[package]\nname = "demo"\n')
        self.write('src/lib.rs', 'pub mod numeric;\npub mod plain;\n')
        self.write('src/numeric.rs', 'pub fn clamp(x: i32) -> i32 { x }\n\n#[cfg(test)]\nmod tests {\n'
                   '    use super::*;\n    #[test]\n    fn clamps() { assert_eq!(clamp(1), 1); }\n}\n')
        self.write('src/plain.rs', 'pub fn bare() {}\n')
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
        edges = self.cli('query', "SELECT s.path AS src, t.path AS dst FROM edges e JOIN nodes s ON s.id = e.source "
                         "JOIN nodes t ON t.id = e.target WHERE e.predicate = 'REFERENCES' ORDER BY 1, 2")['results']
        self.assertEqual({(r['src'], r['dst']) for r in edges},
                         {('package.json', 'scripts/build.mjs'), ('package.json', 'worker'),
                          ('tsconfig.json', 'src'), ('.github/workflows/ci.yml', 'scripts/build.mjs')})
        text = self.raw('map', '--no-primer').split('\n==> ')[0]
        self.assertIn('referenced but missing: .plugin/types/tsconfig.json (named at tsconfig.json:2)', text)
        self.assertIn('referenced but missing: .plugin/types (named at tsconfig.json:3)', text)
        self.assertIn('referenced but missing: scripts/check.sh', text)
        self.assertNotIn('dist/out.js', text)

    def test_type_only_imports_are_marked(self):
        self.write('src/a.ts', "import type { B } from './b';\nexport const a = 1;\nexport type A = B;\n")
        self.write('src/b.ts', "import { a } from './a';\nexport type B = typeof a;\n")
        self.write('src/c.ts', "import { type D } from './d';\nimport { d } from './d';\nexport const c: D = d;\n")
        self.write('src/d.ts', "import { c } from './c';\nexport const d = 1;\nexport type D = number;\nexport const e = c;\n")
        edges = self.cli('query', "SELECT s.path AS src, e.type_only FROM edges e JOIN nodes s ON s.id = e.source "
                         "WHERE e.predicate = 'IMPORTS' ORDER BY 1, 2")['results']
        self.assertIn({'src': 'src/a.ts', 'type_only': 1}, edges)
        self.assertIn({'src': 'src/c.ts', 'type_only': 1}, edges)
        self.assertIn({'src': 'src/c.ts', 'type_only': 0}, edges)

    def test_paging_truncates_without_partial(self):
        self.fixture()
        page = self.cli('map', '--kind', 'file', '--limit', '1')
        self.assertEqual(page['status'], 'ok')
        self.assertFalse(page['delivery']['complete'])
        self.assertIn('results_truncated', page)

    def test_unrelated_parser_gap_is_not_partial(self):
        self.fixture()
        self.write('broken.py', 'def (:\n')
        self.assertEqual(self.cli('map', 'core.py')['status'], 'ok')
        self.assertEqual(self.cli('find', 'save')['status'], 'partial')

    def test_whole_text_is_capped_and_whole_prints_it(self):
        for i in range(8):
            self.write(f'm{i}.py', ''.join(f'value_{n} = {n}  # padding padding padding\n' for n in range(100)))
        text = self.raw('map', '--no-primer')
        self.assertTrue(text.startswith('REPOSITORY SUMMARY'))
        self.assertIn('`map --whole` prints all 8 files', text)
        self.assertTrue(self.raw('map', '--no-primer', '--whole').startswith('WHOLE REPOSITORY'))

    def test_impact_separates_direct_file_only_and_indirect_consumers(self):
        self.write('core.py', 'def save(value):\n    return value\ndef load():\n    return 1\n')
        self.write('saver.py', 'from core import save\ndef run():\n    return save(1)\n')
        self.write('loader.py', 'from core import load\ndef run():\n    return load()\n')
        self.write('api.py', 'from saver import run\ndef go():\n    return run()\n')
        graph = self.graph()
        result = impact(graph, ['core.py::save'])
        reach = {r['path']: r['reach'] for r in result['results'] if r.get('role') == 'possible_consumer'}
        self.assertEqual(reach, {'saver.py': 'direct', 'loader.py': 'file_only', 'api.py': 'indirect'})
        profile = next(r for r in result['results'] if r['kind'] == 'risk_profile')
        self.assertEqual((profile['direct_consumers'], profile['file_only_consumers'], profile['indirect_consumers']),
                         (1, 1, 1))
        whole = impact(graph, ['core.py'])
        self.assertEqual({r['path']: r['reach'] for r in whole['results'] if r.get('role') == 'possible_consumer'},
                         {'saver.py': 'direct', 'loader.py': 'direct', 'api.py': 'indirect'})

    def test_default_rows_are_compact_and_verbose_keeps_identifiers(self):
        self.fixture()
        row = self.cli('map', '--kind', 'file')['results'][0]
        self.assertFalse({'id', 'key', 'digest', 'parse_state'} & set(row))
        self.assertTrue(row['ref'].startswith('snapshot:'))
        full = self.cli('map', '--kind', 'file', '--verbose')['results'][0]
        self.assertTrue({'id', 'key', 'digest'} <= set(full))
