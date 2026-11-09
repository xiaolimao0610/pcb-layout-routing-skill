"""来源绑定的一次性原生位号整理；显式省略只关闭显示，保留逻辑名称与对象。"""
import argparse
import json
import math
from pathlib import Path
from uuid import uuid4

from pcb_bridge import EasyEDABridge
from pcb_native_copper import dispatch, source_identity


def validate_plan(plan):
    """输入同源文字计划，返回原计划；未知字段、重复对象或非法数值抛 ValueError。"""
    if not isinstance(plan, dict) or set(plan) != {'source', 'changes'}:
        raise ValueError('位号计划必须仅含 source 与 changes')
    source_identity(plan['source'])
    if not isinstance(plan['changes'], list) or not 0 < len(plan['changes']) <= 1000:
        raise ValueError('一次位号计划须含 1 至 1000 个已有对象')
    seen = set()
    for item in plan['changes']:
        if not isinstance(item, dict) or item.get('action', 'place') not in ('place', 'omit'):
            raise ValueError('位号动作仅支持 place 或显式 omit')
        omit = item.get('action') == 'omit'
        fields = ({'id', 'parent_id', 'value', 'action', 'reason'} if omit else
                  {'id', 'parent_id', 'value', 'x', 'y', 'rotation', 'font_size', 'line_width'})
        optional = set() if omit else {'align_mode', 'action'}
        if not fields <= set(item) or set(item) - fields - optional:
            raise ValueError('位号改动含缺失或未知字段')
        if any(not isinstance(item[k], str) or not item[k] for k in ('id', 'parent_id', 'value')) or item['id'] in seen:
            raise ValueError('位号标识、父器件或文字无效/重复')
        seen.add(item['id'])
        if omit:
            if not isinstance(item['reason'], str) or not item['reason'].strip():
                raise ValueError('省略丝印显示必须逐对象说明原因')
            continue
        if any(type(item[k]) not in (int, float) or not math.isfinite(item[k]) for k in ('x', 'y', 'rotation', 'font_size', 'line_width')):
            raise ValueError('位号几何必须为有限数字')
        if min(item['font_size'], item['line_width']) <= 0 or item['rotation'] not in (-90, 0, 90, 180):
            raise ValueError('字体尺寸必须为正，候选朝向限 -90/0/90/180 度')
        if 'align_mode' in item and (type(item['align_mode']) is not int or item['align_mode'] not in (3, 5)):
            raise ValueError('只支持已验证的左下对齐3或居中对齐5；省略保持原入口的居中行为')
    return plan


def apply_labels(bridge, plan, window_id, output_dir, *, save=True):
    """输入桥接、已核验来源计划和新审计目录；返回任务收据，未知结果只能查询原键。

    复用原写前本地备份与来源指纹校验；后台仅派发一次，原生也保存备份及任务键。
    写后逐属性双向比对，并核对除目标文字显示字段外的所有原生记录没有变化。
    用户已授权后，可逐对象显式 omit 关闭位号显示；默认 place 不改变可见性。
    API/保存/读取异常记录在 final.errors；不删除逻辑属性，也不授予整板验收。
    """
    validate_plan(plan)
    if type(save) is not bool:
        raise ValueError('save 必须为布尔值')
    payload = dict(plan, job_key='pcb_silkscreen:' + str(uuid4()), save=save)
    expression = r'''(async()=>{
      const p=PAYLOAD;const STRING_ALIGN={LEFT_BOTTOM:3,CENTER:5};
      const alignment=item=>item.align_mode===STRING_ALIGN.LEFT_BOTTOM?STRING_ALIGN.LEFT_BOTTOM:STRING_ALIGN.CENTER;
      await activeDocument(eda,p.source.document_uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('来源已变化');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!==undefined)throw Error('任务键已存在，禁止重放');
      function state(a){return {id:a.getState_PrimitiveId(),parentId:a.getState_ParentPrimitiveId(),key:a.getState_Key(),value:a.getState_Value(),layer:a.getState_Layer(),x:a.getState_X(),y:a.getState_Y(),fontFamily:a.getState_FontFamily(),fontSize:a.getState_FontSize(),lineWidth:a.getState_LineWidth(),alignMode:a.getState_AlignMode(),rotation:a.getState_Rotation(),keyVisible:a.getState_KeyVisible(),valueVisible:a.getState_ValueVisible(),reverse:a.getState_Reverse(),expansion:a.getState_Expansion(),mirror:a.getState_Mirror(),locked:a.getState_PrimitiveLock()};}
      const before=(await eda.pcb_PrimitiveAttribute.getAll()).map(state);
      for(const item of p.changes){const a=before.find(q=>q.id===item.id);if(!a||a.parentId!==item.parent_id||a.value!==item.value||a.key!=='Designator'||!a.valueVisible||a.keyVisible||a.locked||a.layer!==3||a.reverse||a.mirror)throw Error('位号身份、可见性或支持范围不符');}
      const changed=new Set(p.changes.map(a=>a.id));
      const omitted=new Set(p.changes.filter(a=>a.action==='omit').map(a=>a.id));
      function canonical(value){if(Array.isArray(value))return value.map(canonical);if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));return value;}
      function unchangedSource(raw){return JSON.stringify(sourceRows(raw).map(row=>{const h={...row.header},b={...row.payload};if(h.type==='DOCHEAD'){for(const k of ['client','updateTime','version'])delete b[k];}if(h.type==='ATTR'&&changed.has(h.id)){delete h.ticket;const fields=omitted.has(h.id)?['valueVisible']:['x','y','fontSize','strokeWidth','origin','angle'];for(const k of fields)delete b[k];}return JSON.stringify(canonical([h,b]));}).sort());}
      const frozen=unchangedSource(original);
      const job={job_key:p.job_key,status:'pending',source:p.source,success:false,saved:false,applied:false,changed:[],errors:[],full_board_accepted:false};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('任务状态保存失败');};
      if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':backup',{source:original,request:p,attributes:before})!==true)throw Error('写前备份失败');
      await persist();let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{if(invoked)return;invoked=true;return(async()=>{try{
        await activeDocument(eda,p.source.document_uuid);
        if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('派发后来源变化');
        job.status='applying';await persist();
        for(const item of p.changes){
          await activeDocument(eda,p.source.document_uuid);
          const a=await eda.pcb_PrimitiveAttribute.get(item.id);if(!a)throw Error('位号对象消失');
          const edit=a.toAsync();if(item.action==='omit'){edit.setState_ValueVisible(false);}else{edit.setState_X(item.x);edit.setState_Y(item.y);edit.setState_Rotation(item.rotation);edit.setState_FontSize(item.font_size);edit.setState_LineWidth(item.line_width);edit.setState_AlignMode(alignment(item));}
          if(!await edit.done())throw Error('位号修改失败');job.changed.push(item.id);if(job.changed.length%20===0)await persist();
        }
        job.applied=true;if(p.save){job.saved=await eda.pcb_Document.save();if(job.saved!==true)throw Error('修改已完成但保存失败，禁止重放');}
        const sourceBeforeReadback=sourceFingerprint(await readSource(eda));
        const after=(await eda.pcb_PrimitiveAttribute.getAll()).map(state);
        if(after.length!==before.length)throw Error('属性对象数量发生变化');
        const diffs=[];for(const a of before){const b=after.find(q=>q.id===a.id);if(!b){diffs.push(a.id+':缺失');continue;}const item=p.changes.find(q=>q.id===a.id);const wanted=item?(item.action==='omit'?{...a,valueVisible:false}:{...a,x:item.x,y:item.y,rotation:item.rotation,fontSize:item.font_size,lineWidth:item.line_width,alignMode:alignment(item)}):a;for(const k of Object.keys(wanted)){if(typeof wanted[k]==='number'?(typeof b[k]!=='number'||!Number.isFinite(b[k])||Math.abs(wanted[k]-b[k])>0.0001):wanted[k]!==b[k])diffs.push(a.id+':'+k);}}
        const raw=await readSource(eda);if(sourceFingerprint(raw)!==sourceBeforeReadback)throw Error('读回期间来源变化');job.attribute_readback={passed:diffs.length===0,differences:diffs};job.unrelated_source_unchanged=unchangedSource(raw)===frozen;
        job.result_source={document_uuid:p.source.document_uuid,fingerprint:sourceFingerprint(raw)};
        if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':after',{source:raw,attributes:after})!==true)throw Error('写后证据保存失败');
        if(diffs.length||!job.unrelated_source_unchanged)throw Error('写后双向核对存在差异，保留现场，禁止重放');
        job.success=true;
      }catch(error){job.errors.push(String(error));}job.status='final';await persist();})().catch(()=>{});});
      if(accepted!==true){job.status='final';job.errors.push('计时任务被拒绝');await persist();}return job;
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return dispatch(bridge, payload, expression, window_id, output_dir)


def main():
    """接收 apply/status 命令；输出真实收据。读取失败或无效字段抛异常，不重试写入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window-id', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', required=True, type=Path)
    apply.add_argument('--audit-dir', required=True, type=Path)
    status = sub.add_parser('status')
    status.add_argument('--job-key', required=True)
    status.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    bridge = EasyEDABridge()
    if args.command == 'apply':
        result = apply_labels(bridge, json.loads(args.plan.read_text(encoding='utf-8')), args.window_id, args.audit_dir)
    else:
        if not args.job_key.startswith('pcb_silkscreen:'):
            raise ValueError('任务键类型错误')
        result = bridge.execute('return await eda.sys_Storage.getExtensionUserConfig(' + json.dumps(args.job_key) + ');', args.window_id)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
