"""生命周期的离线防护测试；不启动 Docker，不冒充真实隔离验收。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from .environment import EnvironmentError, LocalDocker, TrialEnvironmentManager, TrialEnvironment, WORKSPACE_CONFIG


class EnvironmentOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.docker = Mock()
        self.manager = TrialEnvironmentManager(self.root, Path('/Users/hector/dev/hex-workspace'),
            'benchmark-workspace:offline', 'benchmark-runtime:offline', self.docker)

    def test_remote_daemon_rejected(self):
        for socket in ['tcp://localhost:2375', 'ssh://host', 'unix://relative', '']:
            with self.assertRaises(EnvironmentError): LocalDocker(socket)

    def test_unknown_baseline_and_trial_path_rejected(self):
        with self.assertRaises(EnvironmentError): self.manager.provision('trial', 'unknown')
        with self.assertRaises(ValueError): self.manager.provision('../outside', 'b1b-seed-v1')
        self.docker.run.assert_not_called()

    def test_composition_isolation(self):
        a = self.manager._configuration(self.root / 'a', 'A', 'owner-a')
        b = self.manager._configuration(self.root / 'b', 'B', 'owner-b')
        self.assertEqual(len(a['services']), 7)
        for config in [a, b]:
            for service in config['services'].values():
                self.assertNotIn('container_name', service)
                for port in service.get('ports', []): self.assertTrue(port.startswith('127.0.0.1::'))
            for volume in config['volumes'].values(): self.assertNotIn('external', volume)
        self.assertNotEqual(a['services']['runtime']['volumes'], b['services']['runtime']['volumes'])

    def environment(self):
        from hashlib import sha256
        project='hgb1b-test'; private=self.root/'private'/project; private.mkdir(parents=True)
        (private/'compose.json').write_text('{}')
        (private/'init.sql').write_text('SELECT 1;')
        e=TrialEnvironment('trial', 'b1b-seed-v1', project, 'owner', self.root/'artifacts', private,
                           sha256(b'{}').hexdigest(), self.manager.baseline_digest)
        self.manager.environments[e.trial_id]=e
        self.manager._ownership[e.trial_id]=(e.project,e.owner,e.private_root,e.artifact_root,e.compose_sha256,e.baseline_sha256)
        return e

    def test_tampered_compose_rejected(self):
        e=self.environment(); (e.private_root/'compose.json').write_text('{"volumes":"shared"}')
        with self.assertRaises(EnvironmentError): self.manager.destroy(e)
        self.docker.run.assert_not_called()

    def test_foreign_handle_rejected(self):
        e=self.environment(); self.manager.environments.clear()
        with self.assertRaises(EnvironmentError): self.manager.observe(e)
        self.docker.run.assert_not_called()

    def test_mutated_identity_or_endpoint_rejected(self):
        e=self.environment(); self.manager._endpoints[e.trial_id]={}
        e.endpoints['workspace']='http://127.0.0.1:8098'
        with self.assertRaises(EnvironmentError): self.manager._api(e,'GET','/api/v1/users/me')
        e.endpoints.clear(); e.owner='foreign'
        with self.assertRaises(EnvironmentError): self.manager.destroy(e)
        self.docker.run.assert_not_called()

    def test_unsafe_sql_rejected(self):
        for sql in ['DELETE FROM document', 'SELECT 1; DROP TABLE document']:
            with self.assertRaises(EnvironmentError): self.manager._sql(None, sql)

    def test_owned_cleanup_and_repeated_destroy(self):
        e=self.environment(); self.docker.run.return_value=''
        self.manager.destroy(e); calls=self.docker.run.call_count
        self.manager.destroy(e)
        self.assertEqual(self.docker.run.call_count, calls)
        self.assertTrue(e.destroyed)

    def test_foreign_project_resource_blocks_cleanup(self):
        e=self.environment()
        self.docker.run.side_effect=['foreign-container', json.dumps([{'Config': {'Labels': {
            'com.hexgent.benchmark.owner': 'other'}}}])]
        with self.assertRaises(EnvironmentError): self.manager.destroy(e)
        self.assertFalse(e.destroyed)

    def test_missing_business_state_never_ready(self):
        e=self.environment()
        with patch.object(self.manager, '_health', return_value={'mysql': True}), \
             patch.object(self.manager, 'observe', return_value={'documents': [], 'pending_tasks': {}}):
            result=self.manager.readiness(e, timeout_seconds=.001, poll_seconds=.001)
        self.assertEqual(result['status'], 'INVALID')
        self.assertFalse(result['document_version_ready'])
        self.assertFalse(result['parse_ready']); self.assertFalse(result['vector_ready'])

    def test_polling_limits(self):
        e=self.environment()
        for limit in [0, -1, 601]:
            with self.assertRaises(EnvironmentError): self.manager.readiness(e, timeout_seconds=limit)

    def test_archive_whitelist(self):
        e=self.environment()
        with patch.object(self.manager, '_resources', return_value={'container': [], 'volume': [], 'network': []}):
            self.manager.archive(e)
        text=(e.artifact_root/'environment-config.json').read_text()
        self.assertIn('REDACTED', text)
        self.assertNotIn('DB_PASSWORD', text); self.assertNotIn('USER_TOKEN', text)

    def test_service_config_does_not_target_host_business(self):
        self.assertIn('jdbc:mysql://mysql:3306/', WORKSPACE_CONFIG)
        self.assertIn('http://minio:9000', WORKSPACE_CONFIG)
        self.assertIn('http://qdrant:6333', WORKSPACE_CONFIG)
        self.assertNotIn('127.0.0.1:13306', WORKSPACE_CONFIG)


if __name__ == '__main__':
    unittest.main()
