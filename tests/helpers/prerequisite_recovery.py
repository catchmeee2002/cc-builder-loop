"""Real Core/Git prerequisite recovery; role result envelopes are synthetic."""
from pathlib import Path
from harness import init_repo, commit_all
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.native_driver.cli import _root_session_identity
from tests import test_result_application_contract as application
from tests.test_assurance_v4_contract import contract_for


class PrerequisiteFixture(application.ResultApplicationContractTest):
    def setUp(self, *, mode="automatic_nonsemantic", attempt_limit=3, initial_files=(), root_session=False):
        self.root_session = root_session
        self.repo = init_repo({'src/calc.py': 'def add(a, b):\n    return a + b - 1\n'})
        self.run_id = 'prerequisite-recovery'
        contract = contract_for(self.repo)
        contract['authority']['public_prerequisites'] = ['src/public_a.py', 'src/public_b.py']
        contract['execution'].update(driver_enforced=True, agents={},
            recovery_policy={'schema_version': 1, 'mode': 'automatic_nonsemantic', 'continuation_window': 3})
        if mode == "manual":
            contract['execution']['recovery_policy'] = {'schema_version': 1, 'mode': 'manual'}
        contract['execution']['progress_policy']['attempt_limit'] = attempt_limit
        contract['execution']['work_units'][0]['scope']['paths'] = ['src/**']
        contract['assurance']['required'].insert(2, 'proof')
        self.contract = contract
        runtime = {'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server',
                   'runtime_version': 'fixture', 'protocol_schema_digest': 'a' * 64}
        if root_session:
            contract['execution']['builder_runtime'] = {'schema_version': 1, 'mode': 'root_session'}
            runtime.update(transport='root_session', root_session_identity=_root_session_identity('prerequisite-session'))
        started = core.start(self.repo, self.run_id, 'prerequisite-session', contract, driver_runtime=runtime)
        self.candidate = Path(started['candidate_worktree'])
        if root_session:
            core.prepare_builder(self.repo, self.run_id, 'codex-root-session:prerequisite-session',
                                 owner_mode='root_session', session_id='prerequisite-session')
        else:
            core.prepare_builder(self.repo, self.run_id, 'builder', thread_id='builder-thread')
        self.action = self.begin('builder')
        (self.candidate / 'src/calc.py').write_text('def add(a, b):\n    return sum((a, b))\n')
        for path in initial_files:
            (self.candidate / path).write_text('PUBLIC_VALUE = 1\n')
        commit_all(self.candidate, 'implement calculator before prerequisites')
        self.result = {'result': 'implemented', 'evidence_report': None, 'problem_report': None, 'proof_spec': None}
        self.complete()
        self.apply()

    def begin(self, role):
        if not self.root_session or role != 'builder':
            return super().begin(role)
        action = driver.next_action(self.repo, self.run_id)
        core.begin_dispatch(self.repo, self.run_id, action_id=action['action_id'], action=action['action'],
            role=role, thread_id=None, owner_session_id='prerequisite-session',
            prompt_digest='b' * 64, output_schema_digest='c' * 64,
            work_unit_id=action.get('work_unit_id'), context_projection_digest=action.get('context_projection_digest'))
        return action

    def complete(self):
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=self.result,
            owner_session_id='prerequisite-session' if self.root_session else None)

    def apply(self, result=None):
        if not self.root_session:
            return super().apply(result)
        return core.apply_root_builder_result(self.repo, self.run_id, action_id=self.action['action_id'],
            owner_session_id='prerequisite-session', result_value=result)

    def complete_builder(self, files=()):
        self.action = self.begin('builder')
        for path in files:
            (self.candidate / path).write_text('PUBLIC_VALUE = 1\n')
        if files:
            commit_all(self.candidate, 'complete selected prerequisites')
        self.complete()
