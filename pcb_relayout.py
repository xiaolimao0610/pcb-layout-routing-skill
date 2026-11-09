"""Source-bound independent relayout: explicit cleanup, native outline and poses.

This does not import another PCB, run routing, or claim DRC acceptance. A fresh
canonical export is required after the one-shot native operation.
"""
from argparse import ArgumentParser
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from pcb_bridge import EasyEDABridge
from pcb_native_copper import dispatch, read, source_identity, write


def prepare_plan(inspection, proposal, cleanup):
    source = source_identity(inspection.get("source"))
    if proposal.get("reference_positions_used") is not False:
        raise ValueError("This relayout entry requires explicit reference_positions_used=false for independent poses")
    if set(cleanup) != {"lines", "pours", "vias", "outline_line_ids"}:
        raise ValueError("Cleanup requires explicit lines/pours/vias and outline_line_ids lists")
    seen = set()
    for kind, ids in cleanup.items():
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i or i in seen for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Distinct native IDs are required for every cleanup/outline entry")
        seen.update(ids)
    if not 1 <= len(cleanup["outline_line_ids"]) <= 32:
        raise ValueError("Explicit current native outline LINE IDs are required")
    tracks = {t["id"]: t for t in inspection.get("tracks", [])}
    pours = {p["id"] for p in inspection.get("pours", [])}
    if any(i not in tracks or tracks[i].get("kind") != "line" for i in cleanup["lines"]) or set(cleanup["pours"]) - pours:
        raise ValueError("Cleanup IDs must exist in the source inspection")
    if "initial_poses" in proposal and "poses" in proposal and proposal["initial_poses"] != proposal["poses"]:
        raise ValueError("Conflicting poses and initial_poses")
    poses = proposal.get("initial_poses", proposal.get("poses"))
    if not isinstance(poses, dict) or not poses:
        raise ValueError("Complete independent initial_poses are required")
    for identifier, pose in poses.items():
        if not isinstance(identifier, str) or not identifier or set(pose) != {"x", "y", "rotation", "layer"}:
            raise ValueError("Each native component requires x/y/rotation/layer")
        if any(isinstance(pose[k], bool) or not isinstance(pose[k], (int, float)) or not math.isfinite(pose[k]) for k in ("x", "y", "rotation")) or type(pose["layer"]) is not int or pose["layer"] not in (1, 2):
            raise ValueError("Finite same-side component poses are required")
    bbox = proposal.get("board_bbox", {})
    points = bbox.get("outline")
    if bbox.get("cutouts") or not isinstance(points, list) or len(points) != 4:
        raise ValueError("Relayout currently supports one rectangular outline without cutouts")
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
            raise ValueError("Finite outline coordinate pairs required")
    xs, ys = {p[0] for p in points}, {p[1] for p in points}
    if len(xs) != 2 or len(ys) != 2 or len({tuple(p) for p in points}) != 4 or any((a[0] == b[0]) == (a[1] == b[1]) for a, b in zip(points, points[1:] + points[:1])):
        raise ValueError("Target outline must be a nonzero axis-aligned rectangle in perimeter order")
    layers = inspection.get("copper_layers")
    if not isinstance(layers, list) or not layers or any(type(v) is not int for v in layers) or len(set(layers)) != len(layers):
        raise ValueError("Actual copper layers are required")
    normalized_proposal = deepcopy(proposal)
    normalized_proposal.pop("poses", None)
    normalized_proposal["initial_poses"] = {i: {k: pose[k] for k in ("x", "y", "rotation", "layer")} for i, pose in poses.items()}
    return dict(schema_version=1, mode="independent_native_relayout", source=source,
                outline_mode="replace",
                job_key="pcb_relayout:" + str(uuid4()), copper_layers=deepcopy(layers),
                cleanup=deepcopy(cleanup), proposal=normalized_proposal,
                full_board_accepted=False, requires_reexport=True)


def apply_expression(payload):
    return r'''(async()=>{
      const p=PAYLOAD,uuid=p.source.document_uuid;
      const pose=c=>({x:c.getState_X(),y:c.getState_Y(),rotation:c.getState_Rotation(),layer:c.getState_Layer()});
      const via=v=>({id:v.getState_PrimitiveId(),net:v.getState_Net(),hole:v.getState_HoleDiameter(),diameter:v.getState_Diameter()});
      const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
      const near=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(a-b)<=0.0001;
      const samePose=(a,b)=>near(a.x,b.x)&&near(a.y,b.y)&&a.layer===b.layer&&((a.rotation-b.rotation)%360+360)%360===0;
      const coords=l=>[l.getState_StartX(),l.getState_StartY(),l.getState_EndX(),l.getState_EndY()];
      await activeDocument(eda,uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('Source changed');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!=null)throw Error('Job already exists; query status, never replay');
      const records=inventorySource(original),record=(type,id)=>records.find(r=>r.type===type&&r.id===id);
      const standaloneVias=new Set(records.filter(r=>r.type==='VIA').map(r=>r.id));
      const components=await eda.pcb_PrimitiveComponent.getAll();
      if(!same(components.map(c=>c.getState_PrimitiveId()).sort(),Object.keys(p.proposal.initial_poses).sort()))throw Error('Proposal must cover exactly the current components');
      const originalPoses={};
      for(const c of components){
        const id=c.getState_PrimitiveId(),target=p.proposal.initial_poses[id],before=pose(c);originalPoses[id]=before;
        if(target.layer!==before.layer)throw Error('Component side flips are unsupported');
        if(c.getState_PrimitiveLock()&&!samePose(before,target))throw Error('Locked component would move');
      }
      const liveLayers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE')).map(l=>l.id);
      if(!same([...liveLayers].sort((a,b)=>a-b),[...p.copper_layers].sort((a,b)=>a-b)))throw Error('Copper layers changed');
      const beforeVias=await eda.pcb_PrimitiveVia.getAll(),protectedVias=beforeVias.filter(v=>!standaloneVias.has(v.getState_PrimitiveId())).map(via);
      const lines=await eda.pcb_PrimitiveLine.getAll(),outlines=lines.filter(l=>l.getState_Layer()===11);
      if(!same(outlines.map(l=>l.getState_PrimitiveId()).sort(),[...p.cleanup.outline_line_ids].sort()))throw Error('Current outline is not exactly the explicitly listed LINEs');
      const nonLineGeometry=new Set(['ARC','CARC','POLY','REGION','FILL','PAD','VIA','POUR','POURED','TEARDROP']);
      if(records.some(r=>r.payload?.layerId===11&&nonLineGeometry.has(r.type)))throw Error('Non-LINE outline/cutout needs a separate implementation');
      for(const l of outlines){
        if(l.getState_PrimitiveLock()||l.getState_Net())throw Error('Locked or net-bearing outline');
      }
      const outlineWidths=new Set(outlines.map(l=>l.getState_LineWidth()));
      if(outlineWidths.size!==1)throw Error('Existing outline widths disagree; explicit width policy is required');
      const outlineWidth=[...outlineWidths][0];
      const removals=[...p.cleanup.lines.map(id=>['LINE','Line',id]),...p.cleanup.pours.map(id=>['POUR','Pour',id]),...p.cleanup.vias.map(id=>['VIA','Via',id])];
      const liveObjects={Line:new Map(lines.map(l=>[l.getState_PrimitiveId(),l])),Via:new Map(beforeVias.map(v=>[v.getState_PrimitiveId(),v])),Pour:new Map((await eda.pcb_PrimitivePour.getAll()).map(q=>[q.getState_PrimitiveId(),q]))};
      const removedLineIds=new Set([...p.cleanup.lines,...p.cleanup.outline_line_ids]),derivedTeardrops=new Set(records.filter(r=>r.type==='TEARDROP'&&r.payload.refs.some(id=>removedLineIds.has(id))).map(r=>r.id));
      const removedPourIds=new Set(p.cleanup.pours);
      const pouredParent=r=>{try{const key=JSON.parse(r.id);return Array.isArray(key)&&key.length===2&&key[0]==='POURED'?key[1]:null;}catch{return null;}};
      const derivedPoured=new Set(records.filter(r=>r.type==='POURED'&&removedPourIds.has(pouredParent(r))).map(r=>r.id));
      if(records.some(r=>r.type==='POURED'&&!pouredParent(r)))throw Error('Unproved POURED ownership; stop before cleanup');
      for(const [type,api,id] of removals){
        if(!record(type,id))throw Error('Cleanup target is not an independent source record: '+id);
        const q=liveObjects[api].get(id);
        if(!q||q.getState_PrimitiveLock())throw Error('Cleanup target missing or locked: '+id);
        if(type==='VIA'&&!standaloneVias.has(id))throw Error('Attached via deletion prohibited');
        if(type!=='VIA'&&!liveLayers.includes(q.getState_Layer()))throw Error('Cleanup may only remove named copper primitives');
      }
      const job={job_key:p.job_key,document_uuid:uuid,status:'pending',success:false,saved:false,removed:[],moved:[],outline_updated:[],
        protected_via_ids:protectedVias.map(v=>v.id),derived_teardrops_expected_to_disappear:derivedTeardrops.size,derived_poured_expected_to_disappear:derivedPoured.size,readback_coordinate_tolerance_mil:0.0001,errors:[],preview_only:true,full_board_accepted:false,requires_reexport:true};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Job storage failed');};
      if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':backup',{source:original,request:p})!==true)throw Error('Backup storage failed');
      await persist();let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          try{
            await activeDocument(eda,uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before mutation');
            for(const [type,api,ids] of [['LINE','Line',p.cleanup.lines],['POUR','Pour',p.cleanup.pours],['VIA','Via',p.cleanup.vias]]){
              for(let offset=0;offset<ids.length;offset+=100){
                const batch=ids.slice(offset,offset+100);
                await activeDocument(eda,uuid);
                if(await eda['pcb_Primitive'+api].delete(batch)!==true)throw Error('Batch delete failed: '+api);
                const present=new Set((await eda['pcb_Primitive'+api].getAll()).map(q=>q.getState_PrimitiveId()));
                if(batch.some(id=>present.has(id)))throw Error('Deleted batch still has live objects: '+api);
                job.removed.push(...batch.map(id=>({type,id})));await persist();
              }
            }
            const points=p.proposal.board_bbox.outline;
            await activeDocument(eda,uuid);
            if(await eda.pcb_PrimitiveLine.delete(p.cleanup.outline_line_ids)!==true)throw Error('Old outline deletion failed');
            if((await eda.pcb_PrimitiveLine.getAll()).some(l=>l.getState_Layer()===11))throw Error('Old outline remains; no new outline created');
            job.removed_outline_ids=[...p.cleanup.outline_line_ids];await persist();
            for(let i=0;i<4;i++){
              await activeDocument(eda,uuid);
              const a=points[i],b=points[(i+1)%4];
              const line=await eda.pcb_PrimitiveLine.create('',11,a[0],a[1],b[0],b[1],outlineWidth,false);
              if(!line)throw Error('Outline creation failed');
              job.outline_updated.push(line.getState_PrimitiveId());await persist();
            }
            for(const [id,target] of Object.entries(p.proposal.initial_poses)){
              if(samePose(originalPoses[id],target))continue;
              await activeDocument(eda,uuid);
              const c=await eda.pcb_PrimitiveComponent.get(id);
              if(!c||c.getState_PrimitiveLock())throw Error('Component missing or locked');
              const a=c.toAsync();a.setState_X(target.x);a.setState_Y(target.y);a.setState_Rotation(target.rotation);
              if(!await a.done())throw Error('Component pose write failed: '+id);
              job.moved.push(id);if(job.moved.length%10===0)await persist();
            }
            const verify=async()=>{
              await activeDocument(eda,uuid);
              const afterComponents=await eda.pcb_PrimitiveComponent.getAll();
              if(!same(afterComponents.map(c=>c.getState_PrimitiveId()).sort(),Object.keys(p.proposal.initial_poses).sort()))throw Error('Component inventory changed');
              for(const c of afterComponents)if(!samePose(pose(c),p.proposal.initial_poses[c.getState_PrimitiveId()]))throw Error('Component pose readback mismatch');
              const afterVias=await eda.pcb_PrimitiveVia.getAll();
              for(const expected of protectedVias){const actual=afterVias.find(v=>v.getState_PrimitiveId()===expected.id);if(!actual||!same(via(actual),expected))throw Error('Protected attached via changed or disappeared');}
              const actualLines=await eda.pcb_PrimitiveLine.getAll();
              if(actualLines.filter(l=>l.getState_Layer()===11).length!==4)throw Error('Native outline must contain exactly four new edges');
              for(let i=0;i<4;i++){
                const id=job.outline_updated[i],l=actualLines.find(l=>l.getState_PrimitiveId()===id);
                if(!l||l.getState_Layer()!==11||!coords(l).every((v,k)=>near(v,[...points[i],...points[(i+1)%4]][k]))||l.getState_LineWidth()!==outlineWidth||l.getState_Net()!=='')throw Error('Native outline readback mismatch');
              }
              const present={Line:new Set(actualLines.map(l=>l.getState_PrimitiveId())),Via:new Set(afterVias.map(v=>v.getState_PrimitiveId())),Pour:new Set((await eda.pcb_PrimitivePour.getAll()).map(q=>q.getState_PrimitiveId()))};
              for(const [,api,id] of removals)if(present[api].has(id))throw Error('Deleted primitive remains: '+id);
              const afterRecords=inventorySource(await readSource(eda));
              const residual=afterRecords.filter(r=>r.type==='TEARDROP'&&(derivedTeardrops.has(r.id)||r.payload.refs.some(id=>removedLineIds.has(id))));
              job.residual_derived_teardrops=residual.map(r=>r.id);
              job.residual_derived_poured=afterRecords.filter(r=>r.type==='POURED'&&(derivedPoured.has(r.id)||removedPourIds.has(pouredParent(r)))).map(r=>r.id);
              if(residual.length||job.residual_derived_poured.length){job.requires_cleanup=true;throw Error('Derived copper remains after cleanup; no independent TEARDROP/POURED deletion attempted');}
            };
            await verify();job.applied=true;
            if(p.save){job.saved=await eda.pcb_Document.save();if(job.saved!==true)throw Error('Relayout applied but save failed; do not replay');}
            await verify();job.result_source={document_uuid:uuid,fingerprint:sourceFingerprint(await readSource(eda))};
            job.success=true;
          }catch(error){job.errors.push(String(error));}
          job.status='final';await persist();
        })().catch(()=>{});
      });
      if(accepted!==true){job.status='final';job.errors.push('Timer rejected');await persist();}
      return job;
    })()'''.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False, allow_nan=False))


def apply_plan(bridge, plan, window_id, output_dir, save=False):
    if set(plan) != {"schema_version", "mode", "outline_mode", "source", "job_key", "copper_layers", "cleanup", "proposal", "full_board_accepted", "requires_reexport"} or plan["schema_version"] != 1 or plan["mode"] != "independent_native_relayout" or plan["outline_mode"] != "replace" or plan["full_board_accepted"] is not False or plan["requires_reexport"] is not True:
        raise ValueError("Use prepare to generate a relayout plan")
    if not re.fullmatch(r"pcb_relayout:[0-9a-f-]{36}", plan.get("job_key", "")):
        raise ValueError("Invalid relayout job key")
    # Revalidate all serializable geometry and explicit ID sets before dispatch.
    inspection = {"source": plan["source"], "copper_layers": plan["copper_layers"],
                  "tracks": [{"id": i, "kind": "line"} for i in plan["cleanup"]["lines"]],
                  "pours": [{"id": i} for i in plan["cleanup"]["pours"]]}
    normalized = prepare_plan(inspection, plan["proposal"], plan["cleanup"])
    payload = dict(deepcopy(plan), proposal=normalized["proposal"], save=bool(save))
    return dispatch(bridge, payload, apply_expression(payload), window_id, output_dir)


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--window-id")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ("inspection", "proposal", "cleanup", "output"):
        prep.add_argument("--" + name, required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--audit-dir", required=True)
    apply.add_argument("--save", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--job-key", required=True)
    status.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_plan(read(args.inspection), read(args.proposal), read(args.cleanup))
        write(args.output, result)
    else:
        if not args.window_id:
            parser.error("--window-id is required for native operations")
        bridge = EasyEDABridge(args.base_url)
        if args.command == "apply":
            result = apply_plan(bridge, read(args.plan), args.window_id, args.audit_dir, args.save)
        else:
            if not re.fullmatch(r"pcb_relayout:[0-9a-f-]{36}", args.job_key):
                parser.error("Invalid relayout job key")
            result = bridge.execute("return (await eda.sys_Storage.getExtensionUserConfig(" + json.dumps(args.job_key) + "))??null;", args.window_id)
            if args.output:
                write(args.output, result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result is None or result.get("status") == "final" and not result.get("success") else 0


if __name__ == "__main__":
    raise SystemExit(main())
