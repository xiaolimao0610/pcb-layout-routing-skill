"""Explicit preview-only EDA writeback, with source binding and one-shot jobs.

This never claims DRC acceptance and never retries an uncertain write. Use
pcb_bridge apply for the normal strict transaction and manufacturing workflow.
"""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from pcb_bridge import EasyEDABridge


READBACK_JS = r'''async function previewCopperState(eda){
 const fingerprint=sourceFingerprint(await readSource(eda));
 const layers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE'||l.id===1||l.id===2)).map(l=>l.id).sort((a,b)=>a-b);
 const traces=(await eda.pcb_PrimitiveLine.getAll()).filter(p=>layers.includes(state(p,'Layer'))).map(p=>({
  id:id(p),locked:state(p,'PrimitiveLock'),net:state(p,'Net'),layer:state(p,'Layer'),sx:state(p,'StartX'),sy:state(p,'StartY'),
  ex:state(p,'EndX'),ey:state(p,'EndY'),width:state(p,'LineWidth')}));
 const vias=(await eda.pcb_PrimitiveVia.getAll()).map(p=>{
  if(state(p,'ViaType')!==ENUMS.Via.VIA||state(p,'DesignRuleBlindViaName'))throw Error('Readback requires ordinary complete-stack through vias');
  return {id:id(p),net:state(p,'Net'),x:state(p,'X'),y:state(p,'Y'),hole:state(p,'HoleDiameter'),diameter:state(p,'Diameter'),layers:[...layers]};
 });
 if(sourceFingerprint(await readSource(eda))!==fingerprint)throw Error('Board changed during preview geometry readback');
 return {fingerprint,layers,traces,vias};
}
function previewGeometryComparison(before,after,p){
 const expected=before.traces.filter(t=>!p.remove_traces.includes(t.id)).concat(p.add.traces);
 const expectedVias=p.target_vias??before.vias.concat(p.add.vias);
 const remaining=after.vias.slice(),missingVias=[];
 for(const v of expectedVias){
  const index=remaining.findIndex(a=>a.net===v.net&&['x','y','hole','diameter'].every(k=>near(a[k],v[k]))&&
   JSON.stringify([...a.layers].sort((a,b)=>a-b))===JSON.stringify([...v.layers].sort((a,b)=>a-b)));
  if(index<0)missingVias.push(v);else remaining.splice(index,1);
 }
 const tracesPassed=sameTraceCoverage(expected,after.traces);
 const stackPassed=JSON.stringify(before.layers)===JSON.stringify(after.layers);
 const viasPassed=!missingVias.length&&!remaining.length&&stackPassed;
 return {passed:tracesPassed&&viasPassed,scope:'all_copper_lines_and_through_vias',
  traces_passed:tracesPassed,vias_passed:viasPassed,layer_stack_passed:stackPassed,
  before_fingerprint:before.fingerprint,after_fingerprint:after.fingerprint,
  expected_counts:{traces:expected.length,vias:expectedVias.length},actual_counts:{traces:after.traces.length,vias:after.vias.length},
  missing_trace_coverage:tracesPassed?[]:expected.filter(t=>!traceCoveredByTraces(t,after.traces)),
  unexpected_trace_coverage:tracesPassed?[]:after.traces.filter(t=>!traceCoveredByTraces(t,expected)),
  missing_vias:missingVias,unexpected_vias:remaining};
}
function validatePreviewTraceOperations(operations,before){
 for(const op of operations){
  if(!['modify','create','delete'].includes(op.operation))throw Error('Unsupported trace operation');
  if(op.expected){
   const actual=before.traces.find(t=>t.id===op.id),e=op.expected;
   if(!actual||actual.locked!==false||!before.layers.includes(actual.layer)||actual.net!==e.net||actual.layer!==e.layer||
    !['sx','sy','ex','ey','width'].every(k=>near(actual[k],e[k])))throw Error('Actual source segment mismatch, locked or not copper: '+op.id);
  }
  if(op.replacement&&!before.layers.includes(op.replacement.layer))throw Error('Replacement is not on an actual copper layer');
 }
}'''


TRACE_FIELDS = ("net", "layer", "sx", "sy", "ex", "ey", "width")


def trace_operations(value):
    """Validate a finite, explicit same-net/layer/width line repair whitelist."""
    if not isinstance(value, list) or not value:
        raise ValueError("trace_operations must be a nonempty explicit list")
    expected, replacements, ids = [], [], set()
    for op in value:
        shapes = {"modify": {"operation", "id", "expected", "replacement"},
                  "delete": {"operation", "id", "expected"}, "create": {"operation", "replacement"}}
        if not isinstance(op, dict) or op.get("operation") not in shapes or set(op) != shapes[op["operation"]]:
            raise ValueError("Only explicit modify/create/delete trace operations are supported")
        for field in ("expected", "replacement"):
            if field not in op:
                continue
            t = op[field]
            keys = set(TRACE_FIELDS) | ({"id", "locked"} if field == "expected" else set())
            if (not isinstance(t, dict) or set(t) != keys or not isinstance(t["net"], str) or not t["net"]
                    or type(t["layer"]) is not int or any(isinstance(t[k], bool) or not isinstance(t[k], (int, float))
                    or not math.isfinite(t[k]) for k in TRACE_FIELDS[2:]) or t["width"] <= 0):
                raise ValueError("Trace geometry must contain exact finite mil fields")
        if "expected" in op:
            if (not isinstance(op["id"], str) or not op["id"] or op["id"] in ids
                    or op["expected"]["id"] != op["id"] or op["expected"]["locked"] is not False):
                raise ValueError("Each existing trace must have one unique unlocked expected ID")
            ids.add(op["id"])
            expected.append(op["expected"])
        if "replacement" in op:
            replacements.append(op["replacement"])
        if op["operation"] == "modify" and any(op["expected"][k] != op["replacement"][k] for k in ("net", "layer", "width")):
            raise ValueError("Trace modify must preserve net, layer and width")
    signature = lambda t: (t["net"], t["layer"], t["width"])
    if not expected or {signature(t) for t in expected} != {signature(t) for t in replacements}:
        raise ValueError("Every affected net/layer/width must retain replacement copper; creates cannot introduce another class")
    return deepcopy(value)


def prepare_trace_patch(board, proposal):
    """Bind explicit primitive operations to the complete, freshly exported board."""
    board = board.get("board", board)
    source = board.get("metadata", {}).get("source", board.get("source", {}))
    identity = {key: source.get(key) for key in ("document_uuid", "fingerprint")}
    if (not isinstance(identity["document_uuid"], str) or not identity["document_uuid"]
            or not isinstance(identity["fingerprint"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["fingerprint"])
            or proposal.get("source") != identity or source.get("usage") == "test_only"
            or any(meta.get("usage") == "test_only" or meta.get("board_usage") == "test_only"
                   for meta in (board.get("metadata", {}), proposal.get("metadata", {})))):
        raise ValueError("Proposal and real exported board must have the same original source")
    operations = trace_operations(proposal.get("recommended_primitive_operations"))
    expected = [op["expected"] for op in operations if "expected" in op]
    replacements = [op["replacement"] for op in operations if "replacement" in op]
    if proposal.get("source_segments") != expected or proposal.get("replacement_segments") != replacements:
        # Primitive execution may reorder the same explicit reviewed segments.
        canonical = lambda rows: sorted(json.dumps(row, sort_keys=True, allow_nan=False) for row in rows)
        if canonical(proposal.get("source_segments", [])) != canonical(expected) or canonical(proposal.get("replacement_segments", [])) != canonical(replacements):
            raise ValueError("Operations must describe exactly the proposal source and replacement segments")
    actual = {t["id"]: t for t in board.get("routes", {}).get("traces", [])}
    layers = board.get("rules", {}).get("copper_layers", [])
    nets = set(board.get("nets", []))
    for t in expected:
        current = actual.get(t["id"])
        if (current is None or current.get("locked") is not False or t["layer"] not in layers or t["net"] not in nets
                or any(current.get(k) != t[k] for k in TRACE_FIELDS)):
            raise ValueError("Actual source segment mismatch: " + t["id"])
    return {"source": identity, "trace_operations": operations, "preview_only": True, "full_board_accepted": False}


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def apply_preview(bridge, plan, window, directory, save=False, allow_trace_replacement=False):
    """Explicitly authorized preview; retain exact pre-write source locally."""
    source = plan["source"]
    if (not isinstance(source.get("document_uuid"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", source.get("fingerprint", ""))):
        raise ValueError("A real source UUID and fingerprint are required")
    for meta in (source, plan.get("metadata", {}), plan.get("target_board", {}).get("metadata", {})):
        if meta.get("usage") == "test_only" or meta.get("board_usage") == "test_only" or meta.get("source", {}).get("usage") == "test_only":
            raise ValueError("Test-only geometry cannot be written")
    remove = plan.get("remove", {})
    if remove.get("vias") or remove.get("copper_pours") or (remove.get("traces") and not allow_trace_replacement):
        raise ValueError("Only explicitly enabled trace replacement is supported; vias and fills cannot be removed")
    operations = trace_operations(plan["trace_operations"]) if "trace_operations" in plan else []
    if operations and (not allow_trace_replacement or plan.get("components") or any(remove.values())
                       or any(plan.get("add", {}).values())):
        raise ValueError("trace_operations requires --allow-trace-replacement and cannot mix with other mutations")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    key = "pcb_preview:" + str(uuid4())
    payload = dict(source=deepcopy(source), components=plan.get("components", []),
                   add={"traces": [], "vias": [], "copper_pours": []} if operations else plan["add"],
                   remove_traces=remove.get("traces", []), trace_operations=operations,
                   copper_layers=plan.get("target_board", {}).get("rules", {}).get("copper_layers", []),
                   job_key=key, save=bool(save))
    if payload["components"]:
        target_vias = plan.get("target_board", {}).get("routes", {}).get("vias")
        if target_vias is None:
            raise ValueError("Component moves require complete target_board.routes.vias for attached-via readback")
        payload["target_vias"] = deepcopy(target_vias)
    write(directory / "request.json", payload)
    before = bridge.execute(bridge._adapter_code(
        "(async()=>{await activeDocument(eda," + json.dumps(source["document_uuid"]) + ");"
        "const source=await readSource(eda);return {source,fingerprint:sourceFingerprint(source)};})()"), window)
    (directory / "before-native.txt").write_text(before["source"], encoding="utf-8")
    if before["fingerprint"] != source["fingerprint"]:
        raise ValueError("Source changed; preview was not dispatched")
    expression = "(async()=>{" + READBACK_JS + r'''
      const p=PAYLOAD, uuid=p.source.document_uuid;
      await activeDocument(eda,uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('Source changed');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!==undefined)throw Error('Job already exists');
      const liveLayers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE'||l.id===1||l.id===2)).map(l=>l.id);
      if(p.add.vias.length&&(!Array.isArray(p.copper_layers)||new Set(p.copper_layers).size!==liveLayers.length||
        JSON.stringify([...p.copper_layers].sort((a,b)=>a-b))!==JSON.stringify(liveLayers.sort((a,b)=>a-b))||
        p.add.vias.some(v=>JSON.stringify(v.layers)!==JSON.stringify(p.copper_layers))))throw Error('Preview only supports proven complete-stack through vias');
      if(new Set(p.remove_traces).size!==p.remove_traces.length)throw Error('Duplicate trace replacement');
      for(const id of p.remove_traces){
        const line=await eda.pcb_PrimitiveLine.get(id);
        if(!line||line.getState_PrimitiveLock()||!liveLayers.includes(line.getState_Layer()))throw Error('Replacement trace missing, locked or not copper');
        if(!p.add.traces.some(t=>t.net===line.getState_Net()))throw Error('Replacement has no new trace on its net');
      }
      const beforeCopper=await previewCopperState(eda);
      if(beforeCopper.fingerprint!==p.source.fingerprint)throw Error('Source changed while capturing pre-write copper');
      validatePreviewTraceOperations(p.trace_operations,beforeCopper);
      const readbackRequest=p.trace_operations.length?{remove_traces:p.trace_operations.filter(o=>o.expected).map(o=>o.id),
        add:{traces:p.trace_operations.filter(o=>o.replacement).map(o=>o.replacement),vias:[]}}:p;
      const job={job_key:p.job_key,document_uuid:uuid,status:'pending',preview_only:true,applied:false,saved:false,
        geometry_readback_passed:null,needs_geometry_repair:false,full_board_accepted:false,
        total:{components:p.components.length,traces:p.add.traces.length+p.trace_operations.filter(o=>o.operation==='create').length,vias:p.add.vias.length,
          fills:p.add.copper_pours.reduce((n,q)=>n+q.filled_polygons.length,0),trace_operations:p.trace_operations.length},
        created:{components:0,traces:0,vias:0,fills:0},removed_traces:[],completed_trace_operations:[],errors:[]};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Job storage failed');};
      if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':backup',{source:original,request:p})!==true)throw Error('Backup storage failed');
      await persist();
      let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          try{
            await activeDocument(eda,uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before mutation');
            if(p.trace_operations.length){
              const current=await previewCopperState(eda);
              if(current.fingerprint!==p.source.fingerprint)throw Error('Source changed before trace operations');
              validatePreviewTraceOperations(p.trace_operations,current);
            }
            for(const op of p.trace_operations){
              await activeDocument(eda,uuid);
              const t=op.replacement;let primitive;
              if(op.operation==='modify'){
                primitive=await eda.pcb_PrimitiveLine.modify(op.id,{net:t.net,layer:t.layer,startX:t.sx,startY:t.sy,endX:t.ex,endY:t.ey,lineWidth:t.width});
                if(!primitive)throw Error('Trace modify failed: '+op.id);
              }else if(op.operation==='create'){
                primitive=await eda.pcb_PrimitiveLine.create(t.net,t.layer,t.sx,t.sy,t.ex,t.ey,t.width,false);
                if(!primitive)throw Error('Trace create failed');job.created.traces++;
              }else{
                await eda.pcb_PrimitiveLine.delete(op.id);
                if((await eda.pcb_PrimitiveLine.getAll()).some(t=>id(t)===op.id))throw Error('Trace delete readback failed: '+op.id);
                job.removed_traces.push(op.id);
              }
              job.completed_trace_operations.push({operation:op.operation,id:op.id??id(primitive)});await persist();
            }
            for(const id of p.remove_traces){
              await activeDocument(eda,uuid);
              if(await eda.pcb_PrimitiveLine.delete(id)!==true)throw Error('Trace replacement delete failed');
              job.removed_traces.push(id);await persist();
            }
            for(const c of p.components){
              await activeDocument(eda,uuid);
              if(![c.x,c.y,c.rotation].every(Number.isFinite)||![1,2].includes(c.layer))throw Error('Invalid pose');
              const prim=await eda.pcb_PrimitiveComponent.get(c.id);
              if(!prim||prim.getState_PrimitiveLock())throw Error('Missing or locked component');
              const a=prim.toAsync();a.setState_X(c.x);a.setState_Y(c.y);a.setState_Rotation(c.rotation);a.setState_Layer(c.layer);
              if(!await a.done())throw Error('Component write failed');job.created.components++;await persist();
            }
            for(const t of p.add.traces){
              await activeDocument(eda,uuid);
              if(![t.sx,t.sy,t.ex,t.ey,t.width].every(Number.isFinite)||t.width<=0)throw Error('Invalid trace');
              if(!await eda.pcb_PrimitiveLine.create(t.net,t.layer,t.sx,t.sy,t.ex,t.ey,t.width,false))throw Error('Line write failed');
              job.created.traces++;if(job.created.traces%25===0)await persist();
            }
            for(const v of p.add.vias){
              await activeDocument(eda,uuid);
              if(![v.x,v.y,v.hole,v.diameter].every(Number.isFinite)||v.hole<=0||v.diameter<=v.hole)throw Error('Invalid via');
              if(!await eda.pcb_PrimitiveVia.create(v.net,v.x,v.y,v.hole,v.diameter,ENUMS.Via.VIA,null,null,false))throw Error('Via write failed');
              job.created.vias++;if(job.created.vias%25===0)await persist();
            }
            for(const q of p.add.copper_pours)for(const r of q.filled_polygons){
              await activeDocument(eda,uuid);
              if(r.native_path||r.holes?.length)throw Error('Preview requires decomposed straight fills');
              const poly=eda.pcb_MathPolygon.createPolygon([...r.points[0],'L',...r.points.slice(1).flat()]);
              if(!poly||!await eda.pcb_PrimitiveFill.create(q.layer,poly,q.net,ENUMS.Fill.SOLID,0,false))throw Error('Fill write failed');
              job.created.fills++;if(job.created.fills%10===0)await persist();
            }
            job.applied=true;
            try{if(typeof eda.pcb_Document.zoomToBoardOutline==='function')job.zoomed=await eda.pcb_Document.zoomToBoardOutline();}catch(error){job.display_error=String(error);}
            if(p.save)job.saved=await eda.pcb_Document.save();
            if(p.save&&!job.saved)throw Error('Copper applied but save failed; do not replay');
            job.success=true;
          }catch(error){job.success=false;job.errors.push(String(error));}
          try{
            await activeDocument(eda,uuid);
            job.geometry_readback=previewGeometryComparison(beforeCopper,await previewCopperState(eda),readbackRequest);
            job.geometry_readback_passed=job.geometry_readback.passed;
            job.needs_geometry_repair=!job.geometry_readback_passed;
            if(!job.geometry_readback_passed)job.errors.push('Geometry readback mismatch; inspect current board and prepare a new repair delta; do not replay');
          }catch(error){job.geometry_readback_passed=false;job.needs_geometry_repair=true;job.geometry_readback_error=String(error);job.errors.push('Geometry readback failed: '+String(error));}
          job.status='final';await persist();
        })().catch(()=>{});
      });
      if(accepted!==true){job.status='final';job.success=false;job.errors.push('Timer rejected');await persist();}
      return job;
    })()'''
    expression = expression.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False, allow_nan=False))
    result = bridge.execute(bridge._adapter_code(expression), window)
    write(directory / "dispatch.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--window-id")
    sub = parser.add_subparsers(dest="command", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--audit-dir", required=True)
    apply.add_argument("--ack-preview", action="store_true", required=True)
    apply.add_argument("--save", action="store_true")
    apply.add_argument("--allow-trace-replacement", action="store_true", help="Allow only named copper-line removals paired with replacement traces")
    prepare = sub.add_parser("prepare-trace-patch")
    prepare.add_argument("--board", required=True)
    prepare.add_argument("--proposal", required=True)
    prepare.add_argument("--output", required=True)
    status = sub.add_parser("status")
    status.add_argument("--job-key", required=True)
    status.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "prepare-trace-patch":
        result = prepare_trace_patch(json.loads(Path(args.board).read_text(encoding="utf-8-sig")),
                                     json.loads(Path(args.proposal).read_text(encoding="utf-8-sig")))
        write(args.output, result)
        print(json.dumps({"prepared": True, "trace_operations": len(result["trace_operations"])}))
        return 0
    if not args.window_id:
        parser.error("--window-id is required for live apply/status")
    bridge = EasyEDABridge(args.base_url)
    if args.command == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        result = apply_preview(bridge, plan, args.window_id, args.audit_dir, args.save, args.allow_trace_replacement)
    else:
        if not re.fullmatch(r"pcb_preview:[0-9a-f-]{36}", args.job_key):
            parser.error("invalid preview job key")
        result = bridge.execute("return await eda.sys_Storage.getExtensionUserConfig(" + json.dumps(args.job_key) + ");", args.window_id)
        if args.output:
            write(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if not result or result.get("success") is False or result.get("geometry_readback_passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
