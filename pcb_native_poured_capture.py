"""Read-only Poured unit anchors, calibrated exports, and optional conversion evidence.

Prefer via-anchors: match actual native/API vias to circular antipads without
changing the board. Export rebinds the measured convention to current source.
The optional conversion probe backs up source, converts one region to Fill,
records it, deletes that temporary Fill, and restores its parent. A failed or
timed-out mutation is never replayed.
"""
from argparse import ArgumentParser
import json
from pathlib import Path
from uuid import uuid4

from pcb_bridge import EasyEDABridge
from pcb_native_copper import read, write, source_identity


def calibration_expression(payload):
    return r'''(async()=>{
      const p=PAYLOAD;
      await activeDocument(eda,p.source.document_uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('Source changed before calibration dispatch');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!==undefined)throw Error('Calibration job already exists; never replay');
      const job={job_key:p.job_key,status:'pending',success:false,saved:false,converted:false,temporary_fill_deleted:false,parent_rebuilt:false,errors:[]};
      const evidence={source_before:original,request:p};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Calibration status storage failed');};
      const storeEvidence=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':evidence',evidence)!==true)throw Error('Calibration evidence storage failed');};
      await storeEvidence();await persist();let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          let fillId,convertedSource;
          try{
            await activeDocument(eda,p.source.document_uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before calibration mutation');
            const parent=await eda.pcb_PrimitivePour.get(p.pour_id);
            const poured=parent&&await parent.getCopperRegion();
            if(!poured||poured.getState_PrimitiveId()!==p.poured_id)throw Error('Actual Poured identity changed');
            const region=poured.getState_PourFills().find(f=>f.id===p.region_id);
            if(!region||region.fill!==true||region.lineWidth!==0)throw Error('Actual solid region is absent');
            evidence.raw_paths=nativeCompoundPaths(region.path.getSourceStrictComplex());
            const oldFillIds=new Set((await eda.pcb_PrimitiveFill.getAll()).map(f=>f.getState_PrimitiveId()));
            evidence.existing_fill_ids=[...oldFillIds];await storeEvidence();
            job.status='converting';await persist();
            const fill=await poured.convertToFill(p.region_id);
            if(!fill)throw Error('convertToFill returned no primitive; inspect source, do not replay');
            fillId=fill.getState_PrimitiveId();
            if(oldFillIds.has(fillId))throw Error('Conversion returned an existing Fill; refusing deletion');
            job.converted=true;job.fill_id=fillId;await persist();
            convertedSource=await readSource(eda);evidence.source_converted=convertedSource;
            evidence.fill_api={id:fillId,net:fill.getState_Net(),layer:fill.getState_Layer(),fill_mode:fill.getState_FillMode(),line_width:fill.getState_LineWidth(),path:fill.getState_ComplexPolygon().getSource()};
            await storeEvidence();
            try{
              const added=(await eda.pcb_PrimitiveFill.getAll()).filter(f=>!oldFillIds.has(f.getState_PrimitiveId()));
              if(added.length!==1||added[0].getState_PrimitiveId()!==fillId)throw Error('Conversion did not create exactly one new Fill');
              if(fill.getState_Net()!==parent.getState_Net()||fill.getState_Layer()!==parent.getState_Layer())throw Error('Converted Fill net/layer changed');
              const records=sourceRows(convertedSource).filter(r=>r.header.type==='FILL'&&r.header.id===fillId);
              if(records.length!==1)throw Error('Converted Fill native record is absent or ambiguous');
              evidence.proof=validateConversion(evidence.raw_paths,records[0],p.tolerance_mil);
            }catch(error){job.errors.push(String(error.message||error));}
            await activeDocument(eda,p.source.document_uuid);
            if(sourceFingerprint(await readSource(eda))!==sourceFingerprint(convertedSource))throw Error('Source changed during calibration; inspect before cleanup');
            job.status='restoring';await persist();
            job.delete_return=await eda.pcb_PrimitiveFill.delete(fillId);
            job.removed_verified=!(await eda.pcb_PrimitiveFill.getAll()).some(f=>f.getState_PrimitiveId()===fillId);
            if(!job.removed_verified)throw Error('Temporary Fill remains after deletion');
            job.temporary_fill_deleted=true;await persist();
            if(!(await parent.rebuildCopperRegion()))throw Error('Parent Pour rebuild failed');
            job.parent_rebuilt=true;await persist();
            await activeDocument(eda,p.source.document_uuid);
            if(await eda.pcb_Document.save()!==true)throw Error('Calibration cleanup was applied but save failed');
            job.saved=true;evidence.source_restored=await readSource(eda);
            job.result_source={document_uuid:p.source.document_uuid,fingerprint:sourceFingerprint(evidence.source_restored)};
            if(!job.errors.length){
              const originals=sourceRows(original).filter(r=>r.header.type==='POURED'&&r.payload.pourFill?.some(f=>f.id===p.region_id&&eq(f.path,evidence.raw_paths)));
              if(originals.length!==1)throw Error('Original native Poured identity is ambiguous');
              evidence.profile={source:job.result_source,coordinate_scale:evidence.proof.coordinate_scale,calibration:{method:'native_poured_convert_to_fill',source_before:original,source_converted:convertedSource,poured_id:originals[0].header.id,region_id:p.region_id,fill_id:fillId,raw_paths:evidence.raw_paths,tolerance_mil:p.tolerance_mil}};
              nativePourProfile(evidence.profile,p.source.document_uuid,evidence.source_restored);
              job.success=true;
            }
            job.status=job.success?'completed':'failed';await storeEvidence();await persist();
          }catch(error){
            job.errors.push(String(error.message||error));job.status='failed';job.success=false;
            job.requires_state_inspection=true;
            try{evidence.source_on_error=await readSource(eda);await storeEvidence();}catch(_){}
            await persist();
          }
        })();
      });
      if(accepted!==true)throw Error('Calibration timer was not accepted; query persisted job before taking action');
      return {job_key:p.job_key,status:'pending',local_backup_required:true};
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False, allow_nan=False))


def capture_calibration(bridge, source, pour_id, region_id, output_dir, window_id=None, tolerance_mil=1e-7):
    source = source_identity(source)
    if not all(isinstance(value, str) and value for value in (pour_id, region_id)):
        raise ValueError('Actual native Pour and region IDs are required')
    if isinstance(tolerance_mil, bool) or not isinstance(tolerance_mil, (int, float)) or not 0 < tolerance_mil <= .001:
        raise ValueError('Measured mapping tolerance must be positive and <=0.001 mil')
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    payload = dict(source=source, pour_id=pour_id, region_id=region_id,
                   tolerance_mil=tolerance_mil, job_key='pcb-pour-calibration-' + uuid4().hex)
    write(directory / 'request.json', payload)
    expression = r'''(async()=>{
      const p=PAYLOAD;await activeDocument(eda,p.source.document_uuid);
      const raw=await readSource(eda);
      if(sourceFingerprint(raw)!==p.source.fingerprint)throw Error('Calibration source changed');
      const parent=await eda.pcb_PrimitivePour.get(p.pour_id),poured=parent&&await parent.getCopperRegion();
      if(!poured||!poured.getState_PourFills().some(f=>f.id===p.region_id&&f.fill===true&&f.lineWidth===0))throw Error('Selected actual filled region is absent');
      if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed during calibration backup');
      return {raw,poured_id:poured.getState_PrimitiveId()};
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False))
    backup = bridge.execute(bridge._adapter_code(expression), window_id)
    (directory / 'source-before.epru').write_text(backup['raw'], encoding='utf-8')
    payload['poured_id'] = backup['poured_id']
    write(directory / 'request.json', payload)
    try:
        result = bridge.execute(bridge._adapter_code(calibration_expression(payload)), window_id)
        write(directory / 'dispatch.json', result)
        return dict(result, output_dir=str(directory))
    except Exception as error:
        write(directory / 'dispatch-unknown.json', dict(job_key=payload['job_key'], error=str(error), action='Query this job; never replay conversion'))
        raise


def recovery_expression(payload):
    """Resume evidence/cleanup for one known created Fill; never convert again."""
    return r'''(async()=>{
      const p=PAYLOAD;await activeDocument(eda,p.source.document_uuid);
      const current=await readSource(eda);
      if(sourceFingerprint(current)!==p.source.fingerprint)throw Error('Recovery source changed');
      const original=await eda.sys_Storage.getExtensionUserConfig(p.original_job_key);
      const evidence=await eda.sys_Storage.getExtensionUserConfig(p.original_job_key+':evidence');
      if(!original?.converted||original.temporary_fill_deleted||original.parent_rebuilt||original.recovery_job_key||!original.fill_id||!evidence?.source_converted)throw Error('Original job is not an unrecovered known conversion');
      if(sourceFingerprint(evidence.source_converted)!==p.source.fingerprint)throw Error('Converted document changed; automatic cleanup is not proven');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!==undefined)throw Error('Recovery already exists; never replay');
      const job={job_key:p.job_key,original_job_key:p.original_job_key,status:'pending',success:false,saved:false,converted:true,fill_id:original.fill_id,temporary_fill_deleted:false,parent_rebuilt:false,errors:[]};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Recovery status storage failed');};
      const storeEvidence=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':evidence',evidence)!==true)throw Error('Recovery evidence storage failed');};
      original.recovery_job_key=p.job_key;
      if(await eda.sys_Storage.setExtensionUserConfig(p.original_job_key,original)!==true)throw Error('Recovery guard storage failed');
      await storeEvidence();await persist();let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          try{
            await activeDocument(eda,p.source.document_uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before recovery');
            const request=evidence.request,fillId=job.fill_id;
            if(evidence.existing_fill_ids.includes(fillId))throw Error('Recovery ID was an existing Fill');
            const fill=await eda.pcb_PrimitiveFill.get(fillId),parent=await eda.pcb_PrimitivePour.get(request.pour_id);
            if(!fill||!parent)throw Error('Known converted Fill or original parent is absent');
            job.status='reading_existing_fill';await persist();
            evidence.fill_api={id:fillId,net:fill.getState_Net(),layer:fill.getState_Layer(),fill_mode:fill.getState_FillMode(),line_width:fill.getState_LineWidth(),path:fill.getState_ComplexPolygon().getSource()};
            await storeEvidence();
            try{
              if(fill.getState_Net()!==parent.getState_Net()||fill.getState_Layer()!==parent.getState_Layer())throw Error('Converted Fill net/layer differs');
              const records=sourceRows(evidence.source_converted).filter(r=>r.header.type==='FILL'&&r.header.id===fillId);
              if(records.length!==1)throw Error('Converted native Fill source is ambiguous');
              evidence.proof=validateConversion(evidence.raw_paths,records[0],request.tolerance_mil);
            }catch(error){job.errors.push(String(error.message||error));}
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed while reading recovery evidence');
            job.status='restoring';await persist();
            job.delete_return=await eda.pcb_PrimitiveFill.delete(fillId);
            job.removed_verified=!(await eda.pcb_PrimitiveFill.getAll()).some(f=>f.getState_PrimitiveId()===fillId);
            if(!job.removed_verified)throw Error('Known temporary Fill remains after deletion');
            job.temporary_fill_deleted=true;await persist();
            if(!(await parent.rebuildCopperRegion()))throw Error('Original parent Pour rebuild failed');
            job.parent_rebuilt=true;await persist();
            await activeDocument(eda,p.source.document_uuid);
            if(await eda.pcb_Document.save()!==true)throw Error('Recovery applied but save failed');
            job.saved=true;job.restored=true;evidence.source_restored=await readSource(eda);
            job.result_source={document_uuid:p.source.document_uuid,fingerprint:sourceFingerprint(evidence.source_restored)};
            if(!job.errors.length){
              const originals=sourceRows(evidence.source_before).filter(r=>r.header.type==='POURED'&&r.payload.pourFill?.some(f=>f.id===request.region_id&&eq(f.path,evidence.raw_paths)));
              if(originals.length!==1)throw Error('Original native Poured region identity is ambiguous');
              evidence.profile={source:job.result_source,coordinate_scale:evidence.proof.coordinate_scale,calibration:{method:'native_poured_convert_to_fill',source_before:evidence.source_before,source_converted:evidence.source_converted,poured_id:originals[0].header.id,region_id:request.region_id,fill_id:fillId,raw_paths:evidence.raw_paths,tolerance_mil:request.tolerance_mil}};
              nativePourProfile(evidence.profile,p.source.document_uuid,evidence.source_restored);job.success=true;
            }
            job.status=job.success?'completed':'restored_calibration_unproven';await storeEvidence();await persist();
          }catch(error){job.errors.push(String(error.message||error));job.status='failed';job.success=false;job.requires_state_inspection=true;try{evidence.source_on_recovery_error=await readSource(eda);await storeEvidence();}catch(_){}await persist();}
        })();
      });
      if(accepted!==true)throw Error('Recovery timer not accepted; inspect persisted job');
      return {job_key:p.job_key,status:'pending',conversion_replayed:false};
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False, allow_nan=False))


def recover_calibration(bridge, original_dir, current_source, output_dir, window_id=None):
    current_source = source_identity(current_source)
    original_request = read(Path(original_dir) / 'request.json')
    if original_request['source']['document_uuid'] != current_source['document_uuid']:
        raise ValueError('Recovery must target the same PCB')
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    payload = dict(source=current_source, original_job_key=original_request['job_key'],
                   job_key='pcb-pour-calibration-recovery-' + uuid4().hex)
    write(directory / 'request.json', payload)
    expression = '(async()=>{const p=' + json.dumps(payload) + ';await activeDocument(eda,p.source.document_uuid);const raw=await readSource(eda);if(sourceFingerprint(raw)!==p.source.fingerprint)throw Error("Recovery source changed");return raw;})()'
    raw = bridge.execute(bridge._adapter_code(expression), window_id)
    (directory / 'source-before-recovery.epru').write_text(raw, encoding='utf-8')
    try:
        result = bridge.execute(bridge._adapter_code(recovery_expression(payload)), window_id)
        write(directory / 'dispatch.json', result)
        return dict(result, output_dir=str(directory))
    except Exception as error:
        write(directory / 'dispatch-unknown.json', dict(job_key=payload['job_key'], error=str(error), action='Query this recovery; never replay'))
        raise


def collect_calibration(bridge, output_dir, window_id=None):
    directory = Path(output_dir)
    request = read(directory / 'request.json')
    key = json.dumps(request['job_key'])
    expression = '(async()=>{const k=' + key + ';return {job:await eda.sys_Storage.getExtensionUserConfig(k),evidence:await eda.sys_Storage.getExtensionUserConfig(k+":evidence")};})()'
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    write(directory / 'job.json', result.get('job'))
    evidence = result.get('evidence') or {}
    write(directory / 'evidence.json', evidence)
    for name in ('source_before', 'source_converted', 'source_restored', 'source_on_error'):
        if isinstance(evidence.get(name), str):
            (directory / (name.replace('_', '-') + '.epru')).write_text(evidence[name], encoding='utf-8')
    if evidence.get('profile'):
        write(directory / 'native-pour-profile.json', evidence['profile'])
    return result.get('job')


def export_calibrated_snapshot(bridge, context, profile, output_dir, window_id=None):
    """Fresh live export with a retained measured coordinate convention.

    Source binding is read inside the actual export operation, never copied from
    an old board summary. Each current Poured region is checked against native
    source; the old calibration establishes only the runtime coordinate scale.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    payload = dict(context=context, profile=profile)
    expression = r'''(async()=>{
      const p=PAYLOAD;
      const uuid=p.profile.source.document_uuid;
      await activeDocument(eda,uuid);
      const raw=await readSource(eda);
      const profile={...p.profile,source:{document_uuid:uuid,fingerprint:sourceFingerprint(raw)}};
      delete profile.verification;delete profile.mode;
      const board=await exportBoard(eda,{...p.context,document_uuid:uuid,native_pour_geometry:profile});
      if(board.metadata.source.fingerprint!==sourceFingerprint(raw))throw Error('Source changed during calibrated export');
      return {board,raw};
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False, allow_nan=False))
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    board = result['board']
    (directory / 'native-source.epru').write_text(result['raw'], encoding='utf-8')
    write(directory / 'board.json', board)
    evidence = dict(source=board['metadata']['source'], units='mil', coordinate_frame='native_pcb_data_origin',
                    method=board['metadata']['native_pour_geometry']['calibration']['method'],
                    poured_ids=[p['id'] for p in board['routes']['copper_pours'] if p.get('source_kind') == 'Poured'],
                    coordinate_profile=board['metadata']['native_pour_geometry'],
                    evidence=[board['metadata']['native_pour_geometry']['verification']])
    from pcb_native_poured_geometry import validate_native_pour_snapshot
    validate_native_pour_snapshot(board, evidence)
    write(directory / 'poured-coordinate-evidence.json', evidence)
    return dict(source=evidence['source'], poured_objects=len(evidence['poured_ids']), output_dir=str(directory))


def capture_via_anchor_profile(bridge, source, pour_id, output_dir, window_id=None, region_id=None):
    """Read-only unit calibration from unique circular antipads and real vias."""
    source = source_identity(source)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    payload = dict(source=source, pour_id=pour_id, region_id=region_id)
    write(directory / 'request.json', payload)
    expression = r'''(async()=>{
      const p=PAYLOAD;await activeDocument(eda,p.source.document_uuid);
      const raw=await readSource(eda);
      if(sourceFingerprint(raw)!==p.source.fingerprint)throw Error('Via anchor source changed');
      const parent=await eda.pcb_PrimitivePour.get(p.pour_id),poured=parent&&await parent.getCopperRegion();
      if(!poured)throw Error('Actual parent Pour has no copper');
      const regions=poured.getState_PourFills().filter(f=>f.fill===true&&f.lineWidth===0);
      const region=p.region_id?regions.find(f=>f.id===p.region_id):regions.sort((a,b)=>b.path.getSourceStrictComplex().length-a.path.getSourceStrictComplex().length)[0];
      if(!region)throw Error('Actual filled anchor region is absent');
      const paths=nativeCompoundPaths(region.path.getSourceStrictComplex());
      const candidates=sourceRows(raw).filter(r=>r.header.type==='POURED'&&r.payload.pourFill?.some(f=>f.id===region.id&&eq(f.path,paths)));
      if(candidates.length!==1)throw Error('Actual Poured source identity is ambiguous');
      const vias=(await eda.pcb_PrimitiveVia.getAll()).map(v=>({id:v.getState_PrimitiveId(),net:v.getState_Net(),x:v.getState_X(),y:v.getState_Y(),hole:v.getState_HoleDiameter(),diameter:v.getState_Diameter(),via_type:v.getState_ViaType()}));
      const calibration={method:'native_poured_via_anchors',source_native:raw,pour_id:p.pour_id,poured_id:candidates[0].header.id,region_id:region.id,raw_paths:paths,via_api:vias,tolerance_mil:1e-7};
      const proof=validateViaAnchors(calibration,p.source.document_uuid);
      if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed during via anchor evidence capture');
      const profile={source:p.source,coordinate_scale:proof.coordinate_scale,calibration};
      return {profile,proof,poured_api_id:poured.getState_PrimitiveId()};
    })()'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False, allow_nan=False))
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    write(directory / 'native-pour-profile.json', result['profile'])
    write(directory / 'verification.json', result['proof'])
    write(directory / 'identity.json', dict(poured_api_id=result['poured_api_id'],
                                          poured_source_id=result['profile']['calibration']['poured_id']))
    (directory / 'native-source.epru').write_text(result['profile']['calibration']['source_native'], encoding='utf-8')
    return dict(source=source, coordinate_scale=result['proof']['coordinate_scale'],
                matched_circles=result['proof']['matched_circle_count'], output_dir=str(directory))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--bridge-url')
    parser.add_argument('--window-id')
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('--source', required=True, help='Actual source identity JSON')
    capture.add_argument('--pour-id', required=True)
    capture.add_argument('--region-id', required=True)
    capture.add_argument('--output-dir', required=True)
    capture.add_argument('--tolerance-mil', type=float, default=1e-7)
    collect = commands.add_parser('collect')
    collect.add_argument('--output-dir', required=True)
    recover = commands.add_parser('recover')
    recover.add_argument('--original-dir', required=True)
    recover.add_argument('--source', required=True)
    recover.add_argument('--output-dir', required=True)
    export = commands.add_parser('export')
    export.add_argument('--context', required=True)
    export.add_argument('--profile', required=True)
    export.add_argument('--output-dir', required=True)
    anchors = commands.add_parser('via-anchors')
    anchors.add_argument('--source', required=True)
    anchors.add_argument('--pour-id', required=True)
    anchors.add_argument('--region-id')
    anchors.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    bridge = EasyEDABridge(args.bridge_url)
    if args.command == 'capture':
        result = capture_calibration(bridge, read(args.source), args.pour_id, args.region_id,
                                     args.output_dir, args.window_id, args.tolerance_mil)
    elif args.command == 'collect':
        result = collect_calibration(bridge, args.output_dir, args.window_id)
    elif args.command == 'recover':
        result = recover_calibration(bridge, args.original_dir, read(args.source), args.output_dir, args.window_id)
    elif args.command == 'via-anchors':
        result = capture_via_anchor_profile(bridge, read(args.source), args.pour_id, args.output_dir, args.window_id, args.region_id)
    else:
        result = export_calibrated_snapshot(bridge, read(args.context), read(args.profile), args.output_dir, args.window_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
