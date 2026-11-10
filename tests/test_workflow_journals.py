"""Exact workflow views are independent of transcript ranking and current session exclusions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
try:
    from _engine import storage
except ModuleNotFoundError:  # The engine ships only with the deprecated handtools bundle.
    storage = None


@unittest.skipIf(storage is None, "_engine is not installed")
class WorkflowViews(unittest.TestCase):
    def test_query_loads_exact_project_heads_and_history(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'project'; root.mkdir()
            with patch.dict(os.environ, {'HANDTOOLS_STATE': str(Path(name) / 'state')}):
                base = storage.state_dir('build-skills', root)
                storage.write_state(base / 'workflow.json', {'phase':'draft'})
                storage.write_state(base / 'workflow.json', {'phase':'review', 'sessions':[{'id':'s1', 'plan':{'decisions':[{'id':'D1','question':'Which owner?','answer':'engine'}]}}]})
                script = ROOT / 'scripts' / 'remember.py'
                result = subprocess.run([sys.executable, str(script), '--repo', str(root), 'query',
                    'SELECT run_id, revision, state FROM workflow_runs', '--format', 'json'],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('review', result.stdout)
                self.assertIn('workflow.json', result.stdout)
                result = subprocess.run([sys.executable, str(script), '--repo', str(root), 'query',
                    'SELECT COUNT(*) AS total FROM workflow_events', '--format', 'json'],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('2', result.stdout)
                result = subprocess.run([sys.executable, str(script), '--repo', str(root), 'query',
                    'SELECT question, answer FROM workflow_decisions', '--format', 'json'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Which owner?', result.stdout)
                self.assertIn('engine', result.stdout)
                other = Path(name) / 'other'; other.mkdir()
                result = subprocess.run([sys.executable, str(script), '--repo', str(other), 'query',
                    'SELECT * FROM workflow_runs', '--format', 'json'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('review', result.stdout)


@unittest.skipIf(storage is None or not (ROOT / 'tests' / 'fixtures' / 'recon').is_dir(),
                 "_engine and its recon fixtures are not installed")
class ReconProjections(unittest.TestCase):
    """G5: recon tables are rebuildable flattenings of the journal; decisions come only from the ADR log."""

    def test_rebuild_matches_reduced_state(self):
        import shutil
        from _engine.recon import query, store
        fixtures = ROOT.parent / 'tests' / 'fixtures' / 'recon'
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'orders'
            shutil.copytree(fixtures / 'orders', root)
            with patch.dict(os.environ, {'HANDTOOLS_STATE': str(Path(name) / 'state')}):
                run = store.start(root, 's')['run_id']
                recs = [{'id': 'contract:queue:orders.created', 'classification': 'observed', 'direction': 'outbound',
                         'consumers': [{'unresolved': 'consumer of contract:queue:orders.created'}],
                         'evidence': [{'source': '.', 'path': 'app/events.py', 'statement': 'send orders.created'}]},
                        {'id': 'obs:events-module-per-topic', 'classification': 'inferred', 'claim': 'one publisher per topic',
                         'evidence': [{'source': '.', 'path': 'app/events.py', 'statement': 'publish_order_created'}]}]
                store.record(root, run, {'records': recs}, 1, 'r')
                store.record(root, run, {'records': recs}, 2, 'r')  # duplicate receipt: same request
                store.close(root, run, 2, 'c')
                state, chain, closed = store.read(root, run)
                expected = query.projection(state, len(chain), closed)
                script = ROOT / 'scripts' / 'remember.py'
                def rows(sql):
                    out = subprocess.run([sys.executable, str(script), '--repo', str(root), 'query', sql, '--format',
                                          'json'], capture_output=True, text=True)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    return json.loads(out.stdout)
                def payload(result):
                    return result['results']
                for table, want in expected.items():
                    got = payload(rows(f'SELECT * FROM {table}'))
                    self.assertEqual([{k: v for k, v in r.items() if k != 'source_ref'} for r in got], want, table)
                    self.assertTrue(all(r['source_ref'].endswith('#snap-1:3') for r in got), table)
                contract = payload(rows('SELECT consumers, owner, record_revision FROM recon_contracts'))
                self.assertEqual(contract, [{'consumers': '[{"unresolved": "consumer of contract:queue:orders.created"}]',
                                             'owner': None, 'record_revision': 2}])
                self.assertEqual(payload(rows('SELECT * FROM workflow_decisions')), [])
