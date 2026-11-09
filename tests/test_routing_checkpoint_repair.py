"""计算检查点、累计预算和局部拆线的合成反例；不接触真实 PCB。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcb_routing as routing
from pcb_pipeline_common import PipelineError, check_board, fingerprint, normalize_board
from pcb_route_repair import repair_net, validate_options


def fixture():
    """返回两条可直接连通普通网和完整来源的合成板。"""
    board = dict(units='mil', board_bbox=[0, 0, 200, 110], components=[], nets={}, tags={},
                 routes=dict(traces=[], vias=[], copper_pours=[], shield_vias=[], holes=[], keepouts=[]),
                 rules=dict(copper_layers=[1, 2], minimum_trace_width=2, default_clearance=2,
                            board_edge_clearance=1, component_courtyard_margin=0),
                 metadata=dict(source=dict(document_uuid='checkpoint-test', fingerprint='sha256:'+'a'*64,
                                           usage='test_only')))
    for net, y in (('A', 35), ('B', 75)):
        for number, x in enumerate((20, 180), 1):
            identity = net + str(number)
            board['components'].append(dict(id=identity, x=x, y=y, rotation=0, layer=1, width=4, height=4,
                pads=[dict(padId='1', net=net, localX=0, localY=0, localW=4, localH=4, shape='RECTANGLE')]))
            board['nets'].setdefault(net, []).append(dict(compId=identity, padId='1'))
    return normalize_board(board)


def options(**changes):
    """返回严格可复现的小预算配置。"""
    return dict(dict(width=2, allow_vias=False, trace_layers=[1], grid_step=5, diagonal=True,
                     net_order=['A', 'B'], max_seconds=30, max_net_seconds=15, max_passes=2,
                     checkpoint=True), **changes)


def partial_checkpoint(board=None, config=None):
    """模拟保存 A 后进程退出；返回可恢复缓存，不返回成功板。"""
    saved = []
    def callback(value):
        saved.append(deepcopy(value))
        if value['completed_nets'] == ['A']:
            raise RuntimeError('模拟进程中断')
    with unittest.TestCase().assertRaisesRegex(routing.RoutingFailure, 'CHECKPOINT_WRITE_FAILURE'):
        routing.route_board(board or fixture(), config or options(), checkpoint_callback=callback)
    return saved[-1]


class CheckpointTests(unittest.TestCase):
    def test_resume_skips_completed_net_and_rechecks_whole_geometry(self):
        board, config = fixture(), options()
        before = deepcopy(board)
        checkpoint = partial_checkpoint(board, config)
        self.assertEqual(checkpoint['kind'], 'calculation_checkpoint')
        self.assertFalse(checkpoint['full_board_accepted'])
        self.assertFalse(checkpoint['writeback_allowed'])
        self.assertEqual(checkpoint['pending_nets'], ['B'])
        with patch.object(routing, '_route_net', wraps=routing._route_net) as search:
            result, stats = routing.route_board(board, config, resume_checkpoint=checkpoint)
        self.assertEqual([call.args[1] for call in search.call_args_list], ['B'])
        self.assertEqual(check_board(result), [])
        self.assertEqual(board, before)
        self.assertGreaterEqual(stats['elapsed_seconds'], checkpoint['usage']['elapsed_seconds'])
        self.assertTrue(stats['checkpoint']['resumed'])

    def test_source_configuration_rules_and_pose_changes_invalidate(self):
        checkpoint = partial_checkpoint()
        changes = [('metadata', lambda b: b['metadata']['source'].update(document_uuid='different')),
                   ('rules', lambda b: b['rules'].update(default_clearance=3)),
                   ('pose', lambda b: b['components'][0].update(x=25))]
        for label, change in changes:
            with self.subTest(label=label):
                board = fixture()
                change(board)
                with self.assertRaisesRegex(PipelineError, '已经变化'):
                    routing.route_board(board, options(), resume_checkpoint=checkpoint)
        with self.assertRaisesRegex(PipelineError, '已经变化'):
            routing.route_board(fixture(), options(grid_step=4), resume_checkpoint=checkpoint)

    def test_deleted_trace_false_completion_and_bridge_are_rejected(self):
        baseline = partial_checkpoint()
        for mutation in ('disconnect', 'bridge', 'false_done', 'hide_pending'):
            with self.subTest(mutation=mutation):
                c = deepcopy(baseline)
                if mutation == 'disconnect':
                    c['board']['routes']['traces'] = []
                elif mutation == 'bridge':
                    c['board']['routes']['traces'].append(dict(net='A', layer=1, sx=100, sy=35, ex=100, ey=75, width=2))
                    c['board']['routes']['traces'].append(dict(net='B', layer=1, sx=20, sy=75, ex=180, ey=75, width=2))
                elif mutation == 'false_done':
                    c['completed_nets'], c['pending_nets'] = ['A', 'B'], []
                else:
                    c['pending_nets'] = []
                c['candidate_fingerprint'] = fingerprint(c['board'])
                with self.assertRaises(PipelineError):
                    routing.route_board(fixture(), options(), resume_checkpoint=c)

    def test_original_copper_prefix_cannot_be_rewritten(self):
        board = fixture()
        board['routes']['traces'].append(dict(id='old', net='A', layer=1, sx=20, sy=35, ex=40, ey=35, width=2))
        checkpoint = partial_checkpoint(board)
        checkpoint['board']['routes']['traces'][0]['width'] = 3
        checkpoint['candidate_fingerprint'] = fingerprint(checkpoint['board'])
        with self.assertRaisesRegex(PipelineError, '原铜前缀'):
            routing.route_board(board, options(), resume_checkpoint=checkpoint)

    def test_total_and_per_net_budgets_are_not_reset(self):
        baseline = partial_checkpoint()
        for scope in ('total', 'net'):
            c = deepcopy(baseline)
            c['usage']['elapsed_seconds'] = 31 if scope == 'total' else 16
            c['usage']['net_elapsed_seconds'] = {} if scope == 'total' else {'B': 16}
            c['usage']['expansions'] = 27
            with patch.object(routing, '_route_net', wraps=routing._route_net) as search:
                with self.assertRaisesRegex(routing.RoutingFailure, 'TIME_BUDGET'):
                    routing.route_board(fixture(), options(), resume_checkpoint=c)
            self.assertEqual(search.call_count, 0)

    def test_failed_pass_cannot_be_replayed_indefinitely(self):
        saved = []
        def fail(*args, **kwargs):
            raise routing.RoutingFailure('NO_PATH', '预算内未找到')
        with patch.object(routing, '_route_net', side_effect=fail):
            with self.assertRaises(routing.RoutingFailure):
                routing.route_board(fixture(), options(max_passes=1), checkpoint_callback=saved.append)
        last = saved[-1]
        self.assertEqual(last['pass_index'], 2)
        self.assertEqual(last['failure']['net'], 'A')
        self.assertEqual(last['pending_nets'], ['A', 'B'])
        revalidated = []
        with patch.object(routing, '_route_net') as search:
            with self.assertRaisesRegex(routing.RoutingFailure, 'CHECKPOINT_EXHAUSTED'):
                routing.route_board(fixture(), options(max_passes=1), resume_checkpoint=last,
                                    checkpoint_callback=revalidated.append)
        search.assert_not_called()
        self.assertEqual(len(revalidated), 1)
        self.assertEqual(revalidated[0]['pass_index'], 2)
        self.assertGreaterEqual(revalidated[0]['usage']['elapsed_seconds'], last['usage']['elapsed_seconds'])

    def test_new_fields_are_strict_and_legacy_path_is_unchanged(self):
        for bad in (dict(checkpoint='yes'), dict(local_repair={'unknown': 1}),
                    dict(local_repair={'max_attempts': True})):
            with self.assertRaises(PipelineError):
                routing.algorithm_options(bad)
        with self.assertRaisesRegex(PipelineError, '显式启用'):
            routing.route_board(fixture(), options(checkpoint=False), checkpoint_callback=lambda _: None)
        with self.assertRaisesRegex(PipelineError, '普通网络'):
            routing.route_board(fixture(), options(method='negotiated'))
        with patch.object(routing, '_route_incremental') as incremental:
            result, _ = routing.route_board(fixture(), options(checkpoint=False))
        incremental.assert_not_called()
        self.assertEqual(check_board(result), [])

    def test_escape_is_not_repeated_after_resume(self):
        config = options(escape=dict(enabled=True, groups=[dict(component='A1', pads=['1'], sides=['right'],
                         escape_layers=[1], escape_margin=10, provenance='合成手册关系')]))
        checkpoint = partial_checkpoint(config=config)
        self.assertTrue(checkpoint['escape_completed'])
        self.assertTrue(checkpoint['board']['routes']['traces'])
        import pcb_escape
        with patch.object(pcb_escape, 'escape_board', wraps=pcb_escape.escape_board) as escape:
            result, _ = routing.route_board(fixture(), config, resume_checkpoint=checkpoint)
        escape.assert_not_called()
        self.assertEqual(check_board(result), [])

    def test_deferred_ground_is_visible_and_never_claimed_complete(self):
        config = options(ground_strategy='plane_then_drc', ground_nets=['B'])
        saved = []
        result, stats = routing.route_board(fixture(), config, checkpoint_callback=saved.append)
        self.assertEqual(saved[-1]['deferred_nets'], ['B'])
        self.assertEqual(saved[-1]['completed_nets'], ['A'])
        self.assertFalse(saved[-1]['full_board_accepted'])
        self.assertEqual(len(routing._groups(result, 'B')), 2)
        self.assertEqual(stats['deferred_ground_nets'], ['B'])

    def test_fanout_increment_survives_resume_without_repeating_generation(self):
        from test_pcb_fanout import fixture as bga_fixture, options as bga_options
        import pcb_fanout
        board = bga_fixture()
        board['metadata']['source'] = deepcopy(fixture()['metadata']['source'])
        config = dict(bga_options(), checkpoint=True)
        saved = []
        def interrupt(value):
            saved.append(deepcopy(value))
            raise RuntimeError('在完整扇出后中断')
        with self.assertRaisesRegex(routing.RoutingFailure, 'CHECKPOINT_WRITE_FAILURE'):
            routing.route_board(board, config, checkpoint_callback=interrupt)
        checkpoint = saved[-1]
        self.assertTrue(checkpoint['board']['metadata']['fanout_candidate']['completed'])
        self.assertTrue(checkpoint['board']['routes']['vias'])
        with patch.object(pcb_fanout, 'fanout_board', wraps=pcb_fanout.fanout_board) as fanout:
            result, _ = routing.route_board(board, config, resume_checkpoint=checkpoint)
        fanout.assert_not_called()
        self.assertEqual(check_board(result), [])


class RepairTests(unittest.TestCase):
    def candidate(self):
        board = fixture()
        candidate = deepcopy(board)
        # B 的绕行占据 A 直连通道，但 B 可以改走原本空闲的 y=75 直线。
        for start, end in (((20,75),(20,55)), ((20,55),(100,55)), ((100,55),(100,25)),
                           ((100,25),(165,25)), ((165,25),(165,75)), ((165,75),(180,75))):
            candidate['routes']['traces'].append(dict(net='B', layer=1, sx=start[0], sy=start[1],
                                                      ex=end[0], ey=end[1], width=2))
        self.assertEqual(routing.check_routed_nets(candidate, ['B']), [])
        return board, candidate

    def attempt(self, board, candidate, **updates):
        config = options(checkpoint=False, local_repair=dict(enabled=True, max_attempts=2, radius=30, **updates))
        config = routing.algorithm_options(config)
        settings = {net:routing._net_options(board, net, config) for net in board['nets']}
        stats = dict(expansions=0, connections=0, searches=0)
        result = repair_net(board, candidate, 'A', {'B'}, config, settings, stats, routing._Budget(config))
        return result, stats

    def test_actual_ripup_reroutes_both_nets_and_retains_original(self):
        board, candidate = self.candidate()
        before = deepcopy(candidate)
        result, stats = self.attempt(board, candidate)
        self.assertIsNotNone(result, stats)
        self.assertEqual(check_board(result), [])
        self.assertEqual(candidate, before)
        self.assertEqual(stats['local_repair']['attempts'], 1)
        self.assertEqual(stats['local_repair']['successes'], 1)

    def test_old_copper_cannot_be_selected(self):
        board, candidate = self.candidate()
        board['routes']['traces'] = deepcopy(candidate['routes']['traces'])
        result, stats = self.attempt(board, candidate)
        self.assertIsNone(result)
        self.assertEqual(stats['local_repair']['events'][0]['neighbor_nets'], [])
        self.assertEqual(stats['local_repair']['attempts'], 0)

    def test_failed_trial_rolls_back_and_keeps_complete_netlist(self):
        board, candidate = self.candidate()
        before = deepcopy(candidate)
        def invalid(board, net, *_):
            return deepcopy(board)  # 故意返回断连候选；独立检查必须拒绝。
        with patch.object(routing, '_route_net', side_effect=invalid):
            result, stats = self.attempt(board, candidate)
        self.assertIsNone(result)
        self.assertEqual(candidate, before)
        self.assertEqual(candidate['nets'], board['nets'])
        self.assertIn('DISCONNECTED', str(stats))

    def test_locked_generated_copper_is_protected(self):
        board, candidate = self.candidate()
        candidate['routes']['traces'][0]['locked'] = True
        result, stats = self.attempt(board, candidate)
        self.assertIsNone(result)
        self.assertEqual(stats['local_repair']['attempts'], 0)

    def test_repair_cannot_reset_aggregate_attempt_count(self):
        board, candidate = self.candidate()
        config = routing.algorithm_options(options(local_repair=dict(enabled=True, max_attempts=2)))
        stats = dict(local_repair=dict(attempts=2, successes=0, events=[]))
        result = repair_net(board, candidate, 'A', {'B'}, config, {}, stats, routing._Budget(config))
        self.assertIsNone(result)
        self.assertEqual(stats['local_repair']['attempts'], 2)

    def test_route_entry_uses_local_repair_after_a_bounded_failure(self):
        board, detour = self.candidate()
        real = routing._route_net
        failed_once = False
        def seeded_failure(candidate, net, *args):
            nonlocal failed_once
            if net == 'B' and not candidate['routes']['traces']:
                return deepcopy(detour)
            if net == 'A' and not failed_once:
                failed_once = True
                raise routing.RoutingFailure('NO_PATH', '合成测试：当前顺序候选预算耗尽')
            return real(candidate, net, *args)
        saved = []
        with patch.object(routing, '_route_net', side_effect=seeded_failure):
            result, stats = routing.route_board(board, options(net_order=['B','A'], max_passes=1,
                local_repair=dict(enabled=True, max_attempts=2, radius=30)), checkpoint_callback=saved.append)
        self.assertEqual(check_board(result), [])
        self.assertEqual(stats['local_repair']['successes'], 1)
        self.assertEqual(saved[-1]['usage']['repair_attempts'], 1)
        self.assertEqual(saved[-1]['completed_nets'], ['A','B'])


if __name__ == '__main__':
    unittest.main()
