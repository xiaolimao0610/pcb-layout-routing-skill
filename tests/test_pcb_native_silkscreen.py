"""原生位号入口离线反例；仅 mock 派发及 Node 语法检查，绝不连接桥接或执行JS。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pcb_native_silkscreen as native
from pcb_bridge import EasyEDABridge


def plan():
    """返回明确真实来源格式的隔离测试计划，不包含任何真实工程ID。"""
    return dict(source=dict(document_uuid="fixture-pcb", fingerprint="sha256:" + "a" * 64),
        changes=[dict(id="fixture-attr", parent_id="fixture-component", value="测试R1", x=12.5,
                      y=-4, rotation=90, font_size=20, line_width=3)])


class MockBridge(EasyEDABridge):
    """使用真实适配器打包函数，execute只记录字符串和返回夹具，禁止任何网络。"""
    def __init__(self, output, *, changed=False, timeout=False):
        super().__init__()
        self.output, self.changed, self.timeout = Path(output), changed, timeout
        self.calls = []

    def execute(self, code, window_id=None):
        self.calls.append((code, window_id))
        if len(self.calls) == 1:
            return dict(raw="测试原文备份", fingerprint="sha256:" + ("b" if self.changed else "a") * 64)
        if len(self.calls) != 2:
            raise AssertionError("不得自动重试写入")
        if not (self.output / "request.json").is_file() or not (self.output / "before-native.epru").is_file():
            raise AssertionError("派发前必须已保存请求和原文备份")
        if self.timeout:
            raise TimeoutError("模拟结果未知，只能查询任务键")
        return dict(status="pending", success=False, full_board_accepted=False)


class NativeSilkscreenTests(unittest.TestCase):
    def test_unknown_and_hide_fields_rejected(self):
        for field in ("visible", "valueVisible", "keyVisible", "hidden", "layer", "delete"):
            value = plan()
            value["changes"][0][field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                native.validate_plan(value)
        value = plan()
        value["options"] = {}
        with self.assertRaises(ValueError):
            native.validate_plan(value)

    def test_omit_requires_explicit_action_reason_and_no_geometry(self):
        value = plan()
        value['changes'] = [dict(id='fixture-attr', parent_id='fixture-component',
                                 value='测试R1', action='omit', reason='有限排布仍压焊盘，用户允许省略')]
        self.assertEqual(native.validate_plan(value), value)
        for change in ({'reason': ''}, {'reason': '  '}, {'reason': False},
                       {'action': 'delete'}, {'action': True}, {'x': 10},
                       {'valueVisible': False}):
            invalid = deepcopy(value)
            invalid['changes'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.validate_plan(invalid)
        for field in ('action', 'reason'):
            invalid = deepcopy(value)
            del invalid['changes'][0][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                native.validate_plan(invalid)
        explicit_place = plan()
        explicit_place['changes'][0]['action'] = 'place'
        self.assertEqual(native.validate_plan(explicit_place), explicit_place)

    def test_duplicate_nan_infinite_and_boolean_rejected(self):
        value = plan()
        value["changes"].append(deepcopy(value["changes"][0]))
        with self.assertRaises(ValueError):
            native.validate_plan(value)
        for key in ("x", "y", "rotation", "font_size", "line_width"):
            for bad in (float("nan"), float("inf"), True):
                value = plan()
                value["changes"][0][key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    native.validate_plan(value)

    def test_source_binding_and_rotation_scope(self):
        for change in (dict(usage="test_only"), dict(fingerprint="not-a-fingerprint"), dict(other="x")):
            value = plan()
            value["source"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.validate_plan(value)
        value = plan()
        value["changes"][0]["rotation"] = 45
        with self.assertRaises(ValueError):
            native.validate_plan(value)

    def test_dispatch_reuses_backup_and_never_replays_directory(self):
        value, original = plan(), plan()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "audit"
            bridge = MockBridge(output)
            result = native.apply_labels(bridge, value, "fixture-window", output)
            self.assertEqual(len(bridge.calls), 2)  # 一次只读备份、一次写任务派发。
            self.assertEqual(result["status"], "pending")
            self.assertEqual((output / "before-native.epru").read_text(encoding="utf-8"), "测试原文备份")
            request = json.loads((output / "request.json").read_text(encoding="utf-8"))
            self.assertTrue(request["job_key"].startswith("pcb_silkscreen:"))
            self.assertEqual(request["changes"], original["changes"])
            self.assertTrue(request["save"])
            self.assertTrue((output / "dispatch.json").is_file())
            with self.assertRaises(FileExistsError):
                native.apply_labels(bridge, value, "fixture-window", output)
            self.assertEqual(len(bridge.calls), 2)
            self.assertEqual(value, original)

    def test_explicit_original_alignment_and_orthogonal_rotation(self):
        for rotation in (-90, 0, 90, 180):
            value = plan()
            value['changes'][0].update(rotation=rotation, align_mode=3)
            self.assertEqual(native.validate_plan(value), value)
        for bad in (True, 3.0, 0, 6, '3'):
            value = plan()
            value['changes'][0]['align_mode'] = bad
            with self.subTest(align_mode=bad), self.assertRaises(ValueError):
                native.validate_plan(value)

    def test_changed_source_never_dispatches_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "audit"
            bridge = MockBridge(output, changed=True)
            with self.assertRaises(ValueError):
                native.apply_labels(bridge, plan(), "fixture-window", output)
            self.assertEqual(len(bridge.calls), 1)
            self.assertTrue((output / "before-native.epru").is_file())
            self.assertFalse((output / "dispatch.json").exists())

    def test_unknown_outcome_keeps_request_backup_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "audit"
            bridge = MockBridge(output, timeout=True)
            with self.assertRaises(TimeoutError):
                native.apply_labels(bridge, plan(), "fixture-window", output)
            self.assertEqual(len(bridge.calls), 2)
            self.assertTrue((output / "request.json").is_file())
            self.assertTrue((output / "before-native.epru").is_file())
            self.assertFalse((output / "dispatch.json").exists())

    def test_generated_javascript_syntax_only(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("未安装Node，需另行执行生成脚本的 --check，不冒充已检查")
        value = plan()
        value["changes"][0]["value"] = "R引号'\"与换行\n"  # JSON编码不能造成脚本注入或丢字。
        with patch.object(native, "dispatch", return_value={}) as dispatched:
            native.apply_labels(object(), value, "fixture-window", "unused", save=False)
        dispatched.assert_called_once()
        expression = dispatched.call_args.args[2]
        self.assertEqual(expression.count("setTimeoutTimer("), 1)
        for forbidden in ("setState_Value(", "setState_Key(",
                          "setState_KeyVisible(", "pcb_PrimitiveAttribute.delete("):
            self.assertNotIn(forbidden, expression)
        self.assertIn("if(item.action==='omit'){edit.setState_ValueVisible(false);}", expression)
        self.assertIn("Number.isFinite(b[k])", expression)
        self.assertIn("sourceBeforeReadback", expression)
        self.assertIn("unrelated_source_unchanged", expression)
        # 仅解析语法。即使适配器含真实EDA函数，node --check也不会执行。
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "generated.js"
            wrapped = "async function syntaxOnly(){\n" + EasyEDABridge()._adapter_code(expression) + "\n}\n"
            script.write_text(wrapped, encoding="utf-8")
            for path in (script, ROOT / "eda_adapter.mjs"):
                completed = subprocess.run([node, "--check", str(path)], capture_output=True,
                                           text=True, encoding="utf-8", timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_omit_mock_exec_preserves_logic_and_rejects_collateral_changes(self):
        """隔离执行生成JS：正确省略通过；额外移动、改名及非目标隐藏必须被读回拒绝。"""
        node = shutil.which('node')
        if node is None:
            self.skipTest('未安装Node，隔离执行未验证')
        value = plan()
        value['changes'] = [dict(id='fixture-attr', parent_id='fixture-component',
                                 value='测试R1', action='omit', reason='用户允许省略拥挤位号')]
        with patch.object(native, 'dispatch', return_value={}) as dispatched:
            native.apply_labels(object(), value, 'fixture-window', 'unused')
        expression = dispatched.call_args.args[2]
        harness = r'''
const mode=process.argv[2], stored=new Map();let pending;
const original={id:'fixture-attr',parentId:'fixture-component',key:'Designator',value:'测试R1',layer:3,x:2,y:3,fontFamily:'default',fontSize:45,lineWidth:6,alignMode:3,rotation:0,keyVisible:false,valueVisible:true,reverse:false,expansion:0,mirror:false,locked:false};
const states=[{...original},{...original,id:'untouched-attr',parentId:'untouched-component',value:'R2'}];
function attr(s){const a={};for(const [api,key] of Object.entries({PrimitiveId:'id',ParentPrimitiveId:'parentId',Key:'key',Value:'value',Layer:'layer',X:'x',Y:'y',FontFamily:'fontFamily',FontSize:'fontSize',LineWidth:'lineWidth',AlignMode:'alignMode',Rotation:'rotation',KeyVisible:'keyVisible',ValueVisible:'valueVisible',Reverse:'reverse',Expansion:'expansion',Mirror:'mirror',PrimitiveLock:'locked'})){a['getState_'+api]=()=>s[key];a['setState_'+api]=v=>{s[key]=v;};}a.toAsync=()=>a;a.setState_ValueVisible=v=>{s.valueVisible=v;if(mode==='move')s.x+=1;if(mode==='rename')s.value='WRONG';if(mode==='hide-other')states[1].valueVisible=false;};a.done=async()=>{if(mode==='place-hide')s.valueVisible=false;return true;};return a;}
const eda={pcb_PrimitiveAttribute:{getAll:async()=>states.map(attr),get:async id=>attr(states.find(s=>s.id===id))},pcb_Document:{save:async()=>true},sys_Storage:{getExtensionUserConfig:async k=>stored.get(k),setExtensionUserConfig:async(k,v)=>{stored.set(k,JSON.parse(JSON.stringify(v)));return true;}},sys_Timer:{setTimeoutTimer:(key,ms,fn)=>{pending=fn();return true;}}};
async function activeDocument(){}
async function readSource(){return JSON.stringify(states.map(s=>({header:{type:'ATTR',id:s.id,ticket:1},payload:{...s,strokeWidth:s.lineWidth,origin:s.alignMode,angle:s.rotation}})));}
function sourceRows(raw){return JSON.parse(raw);}
function sourceFingerprint(){return 'sha256:'+'a'.repeat(64);}
await EXPRESSION;await pending;
const job=[...stored.values()].find(x=>x.status==='final');
console.log(JSON.stringify({job,states}));
'''.replace('EXPRESSION', expression)
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'isolated.mjs'
            script.write_text(harness, encoding='utf-8')
            for mode in ('correct', 'move', 'rename', 'hide-other'):
                completed = subprocess.run([node, str(script), mode], capture_output=True,
                                           text=True, encoding='utf-8', timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                with self.subTest(mode=mode):
                    self.assertEqual(result['job']['success'], mode == 'correct')
                    self.assertFalse(result['job']['full_board_accepted'])
                    if mode == 'correct':
                        self.assertFalse(result['states'][0]['valueVisible'])
                        self.assertEqual(result['states'][0]['value'], '测试R1')
                        self.assertEqual(result['states'][0]['x'], 2)
                        self.assertTrue(result['states'][1]['valueVisible'])
                        self.assertTrue(result['job']['attribute_readback']['passed'])
                        self.assertTrue(result['job']['unrelated_source_unchanged'])
                    else:
                        self.assertFalse(result['job']['attribute_readback']['passed'])
                        self.assertFalse(result['job']['unrelated_source_unchanged'])
            # 普通place仍不能借omit白名单意外关闭自己的显示。
            with patch.object(native, 'dispatch', return_value={}) as placed:
                native.apply_labels(object(), plan(), 'fixture-window', 'unused')
            script.write_text(harness.replace(expression, placed.call_args.args[2]), encoding='utf-8')
            completed = subprocess.run([node, str(script), 'place-hide'], capture_output=True,
                                       text=True, encoding='utf-8', timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertFalse(result['job']['success'])
            self.assertFalse(result['job']['attribute_readback']['passed'])
            self.assertFalse(result['job']['unrelated_source_unchanged'])


if __name__ == "__main__":
    unittest.main()
