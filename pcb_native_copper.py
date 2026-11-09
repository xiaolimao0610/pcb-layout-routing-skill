"""Native plane-first grounding: pour, inspect real copper, then target DRC gaps.

This entry point does not route the entire ground net or interpret DRC display
positions as PCB coordinates. It preserves native curved copper paths verbatim.
Operation success, saved state, native DRC, and ground diagnostics are separate.
"""
from argparse import ArgumentParser
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from shapely.geometry import Polygon
from pcb_bridge import EasyEDABridge


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def source_identity(value):
    value = deepcopy(value)
    if (not isinstance(value, dict) or not isinstance(value.get("document_uuid"), str)
            or not value["document_uuid"] or not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("fingerprint", ""))
            or set(value) - {"document_uuid", "fingerprint", "usage"}
            or value.get("usage") not in (None, "physical")):
        raise ValueError("A real PCB source UUID and fingerprint are required; test-only sources are not writable")
    return value


def copper_keepouts(value, known_layers):
    """Validate explicit physical exclusion polygons; never infer antenna margins."""
    if not isinstance(value, list):
        raise ValueError("copper_keepouts must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"points", "layers", "provenance"}:
            raise ValueError("A copper keepout requires points, actual layers and provenance")
        points, layers = item["points"], item["layers"]
        if (not isinstance(points, list) or len(points) < 3
                or any(not isinstance(p, list) or len(p) != 2 or any(isinstance(v, bool)
                       or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in points)):
            raise ValueError("Copper keepout points must be finite mil coordinate pairs")
        if (not isinstance(layers, list) or not layers or any(type(layer) is not int for layer in layers)
                or len(set(layers)) != len(layers) or set(layers) - set(known_layers)):
            raise ValueError("Copper keepout layers must be distinct actual copper layer IDs")
        if not isinstance(item["provenance"], str) or not item["provenance"].strip():
            raise ValueError("Copper keepout provenance must explain the declared physical region")
        shape = Polygon(points)
        if not shape.is_valid or shape.is_empty or shape.area <= 0:
            raise ValueError("Copper keepout must be a valid nonempty simple polygon")
        result.append((deepcopy(item), shape))
    return result


def prepare_plan(board, spec):
    """Create from a real snapshot, or refresh named pours from inspect output."""
    board = board.get("board", board)
    source = source_identity(board.get("metadata", {}).get("source", board.get("source", {})))
    if board.get("metadata", {}).get("board_usage") == "test_only":
        raise ValueError("Test-only board geometry cannot be poured")
    if set(spec) - {"layers", "edge_clearance", "ground_nets", "copper_keepouts"}:
        raise ValueError("Unknown native copper specification field")
    layers = spec.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("layers must explicitly list layer/net pairs or existing pour IDs")
    known_layers = board.get("rules", {}).get("copper_layers", board.get("copper_layers", []))
    known_nets = set(board.get("nets", []))
    if not known_layers or not known_nets:
        raise ValueError("Snapshot/inspection must include actual copper layers and nets")
    clearance = spec.get("edge_clearance", board.get("rules", {}).get("board_edge_clearance", 20))
    if isinstance(clearance, bool) or not isinstance(clearance, (int, float)) or not math.isfinite(clearance) or clearance < 0:
        raise ValueError("edge_clearance must be a nonnegative mil distance")
    ground_nets = spec.get("ground_nets", ["GND"])
    if not isinstance(ground_nets, list) or not ground_nets or len(set(ground_nets)) != len(ground_nets) or set(ground_nets) - known_nets:
        raise ValueError("ground_nets must name actual distinct networks")
    exclusions = copper_keepouts(spec.get("copper_keepouts", []), known_layers)
    inset = None
    result = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or set(layer) - {"layer", "net", "name", "pour_id"}:
            raise ValueError("Each pour requires layer/net, with optional name and existing pour_id")
        if type(layer.get("layer")) is not int or layer["layer"] not in known_layers or layer.get("net") not in known_nets:
            raise ValueError("Pour layer or net is not present in the real PCB")
        item = dict(layer=layer["layer"], net=layer["net"], name=layer.get("name", "Copper_" + str(index + 1)))
        if not isinstance(item["name"], str) or not item["name"]:
            raise ValueError("Pour name must be nonempty")
        if layer.get("pour_id"):
            if not isinstance(layer["pour_id"], str):
                raise ValueError("pour_id must be a native primitive ID")
            if any(layer["layer"] in item["layers"] for item, _ in exclusions):
                raise ValueError("A refresh preserves its existing border; rebuild its explicit region to apply copper keepouts")
            item["pour_id"] = layer["pour_id"]
        else:
            if inset is None:
                outline = board.get("board_bbox")
                if not isinstance(outline, dict) or not outline.get("outline"):
                    raise ValueError("Creating pours needs a real outline snapshot; inspect supports refresh only")
                physical_board = Polygon(outline["outline"], outline.get("cutouts", []))
                if not physical_board.is_valid or physical_board.is_empty or physical_board.area <= 0:
                    raise ValueError("A valid actual board outline is required")
                inset = physical_board.buffer(-clearance, join_style=2)
            region = inset
            for exclusion, shape in exclusions:
                if layer["layer"] in exclusion["layers"]:
                    region = region.difference(shape)
            if region.is_empty or region.geom_type != "Polygon" or not region.is_valid or region.interiors:
                raise ValueError("Layer inset minus copper keepouts is empty, split, or has holes; explicit region planning is required")
            item["points"] = [list(point) for point in list(region.exterior.coords)[:-1]]
        result.append(item)
    existing_ids = [item["pour_id"] for item in result if "pour_id" in item]
    if len(set(existing_ids)) != len(existing_ids):
        raise ValueError("An existing pour may be refreshed only once per plan")
    plan = dict(schema_version=1, source=source, copper_layers=list(known_layers), layers=result,
                ground_nets=ground_nets, edge_clearance=clearance, preserve_islands=False,
                mode="native_plane_then_drc", full_board_accepted=False)
    if exclusions:
        plan["copper_keepouts"] = [item for item, _ in exclusions]
    validate_plan(plan)
    return plan


def validate_plan(plan):
    required = {"schema_version", "source", "copper_layers", "layers", "ground_nets", "edge_clearance", "preserve_islands", "mode", "full_board_accepted"}
    if not isinstance(plan, dict) or not required <= set(plan) or set(plan) - required - {"copper_keepouts"}:
        raise ValueError("Use prepare to generate a native copper plan")
    source_identity(plan["source"])
    if plan["mode"] != "native_plane_then_drc" or plan["schema_version"] != 1 or plan["preserve_islands"] is not False or plan["full_board_accepted"] is not False:
        raise ValueError("Unsupported native copper plan mode")
    layers = plan["copper_layers"]
    if not isinstance(layers, list) or not layers or any(type(layer) is not int for layer in layers) or len(set(layers)) != len(layers):
        raise ValueError("Distinct actual copper layer IDs required")
    if not isinstance(plan["ground_nets"], list) or not plan["ground_nets"] or any(not isinstance(net, str) or not net for net in plan["ground_nets"]):
        raise ValueError("Explicit ground network names required")
    if not isinstance(plan["layers"], list) or not plan["layers"]:
        raise ValueError("At least one explicit pour is required")
    exclusions = copper_keepouts(plan.get("copper_keepouts", []), layers)
    seen_ids, create_layers = set(), set()
    for item in plan["layers"]:
        if not isinstance(item, dict) or set(item) not in ({"layer", "net", "name", "points"}, {"layer", "net", "name", "pour_id"}):
            raise ValueError("Pour requires exactly one of points or pour_id")
        if type(item["layer"]) is not int or item["layer"] not in layers or any(not isinstance(item[key], str) or not item[key] for key in ("net", "name")):
            raise ValueError("Invalid pour layer, net or name")
        if "pour_id" in item:
            if not isinstance(item["pour_id"], str) or not item["pour_id"] or item["pour_id"] in seen_ids:
                raise ValueError("Duplicate or invalid refresh ID")
            if any(item["layer"] in exclusion["layers"] for exclusion, _ in exclusions):
                raise ValueError("Refresh cannot apply a new copper exclusion")
            seen_ids.add(item["pour_id"])
        else:
            points = item["points"]
            if (not isinstance(points, list) or len(points) < 3 or any(not isinstance(point, list) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point) for point in points)):
                raise ValueError("Finite polygon coordinate pairs required")
            polygon = Polygon(points)
            if not polygon.is_valid or polygon.area <= 0 or item["layer"] in create_layers:
                raise ValueError("Invalid polygon or duplicate whole-layer pour creation")
            if any(item["layer"] in exclusion["layers"] and polygon.intersection(shape).area > 0
                   for exclusion, shape in exclusions):
                raise ValueError("Prepared copper polygon overlaps its declared keepout")
            create_layers.add(item["layer"])


# Native paths are evidence, not chord-only approximations passed to the offline solver.
READ_COPPER_JS = r'''async function readCopper(ids){
 const result=[];
 for(const id of ids){
  const p=await eda.pcb_PrimitivePour.get(id);
  if(!p)throw Error('Pour disappeared: '+id);
  const c=await p.getCopperRegion();
  result.push({id,poured_id:c&&typeof c.getState_PrimitiveId==='function'?c.getState_PrimitiveId():null,
   net:p.getState_Net(),layer:p.getState_Layer(),name:p.getState_PourName(),
   preserve_islands:p.getState_PreserveSilos(),
   regions:c?c.getState_PourFills().map(r=>({id:r.id,fill:r.fill,lineWidth:r.lineWidth,path:r.path.getSourceStrictComplex()})):[]});
 }
 return result;
}'''

READ_TRACKS_JS = r'''function trackState(p,kind){
 const t={id:p.getState_PrimitiveId(),kind,net:p.getState_Net(),layer:p.getState_Layer(),
   sx:p.getState_StartX(),sy:p.getState_StartY(),ex:p.getState_EndX(),ey:p.getState_EndY(),
   width:p.getState_LineWidth(),locked:p.getState_PrimitiveLock()};
 if(kind==='arc')t.angle=p.getState_ArcAngle();
 return t;
}
async function readTracks(layers){
 const lines=(await eda.pcb_PrimitiveLine.getAll()).filter(p=>layers.includes(p.getState_Layer())).map(p=>trackState(p,'line'));
 const arcs=(await eda.pcb_PrimitiveArc.getAll()).filter(p=>layers.includes(p.getState_Layer())).map(p=>trackState(p,'arc'));
 return [...lines,...arcs];
}'''


def inspect_board(bridge, document_uuid, window_id, output_dir):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    expression = "(async()=>{" + READ_COPPER_JS + READ_TRACKS_JS + "await activeDocument(eda," + json.dumps(document_uuid) + ");" + r'''
      const raw=await readSource(eda);
      const document=await eda.dmt_SelectControl.getCurrentDocumentInfo();
      const layers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE'));
      const pours=await readCopper((await eda.pcb_PrimitivePour.getAll()).map(p=>p.getState_PrimitiveId()));
      const nets=await eda.pcb_Net.getAllNetsName();
      const tracks=await readTracks(layers.map(l=>l.id));
      if(sourceFingerprint(await readSource(eda))!==sourceFingerprint(raw))throw Error('Source changed during inspection');
      return {source:{document_uuid:document.uuid,fingerprint:sourceFingerprint(raw)},
        copper_layers:layers.map(l=>l.id),layers,nets,pours,tracks,raw};
    })()'''
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    (directory / "native-source.epru").write_text(result.pop("raw"), encoding="utf-8")
    write(directory / "inspection.json", result)
    return {"source": result["source"], "pours": len(result["pours"]), "output_dir": str(directory)}


def apply_expression(payload):
    return "(async()=>{" + READ_COPPER_JS + r'''
      const p=PAYLOAD;
      await activeDocument(eda,p.source.document_uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('Source changed');
      if(await eda.sys_Storage.getExtensionUserConfig(p.job_key)!==undefined)throw Error('Job already exists');
      const liveLayers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE'));
      if(JSON.stringify(liveLayers.map(l=>l.id).sort((a,b)=>a-b))!==JSON.stringify([...p.copper_layers].sort((a,b)=>a-b)))throw Error('Copper layer set changed');
      const nets=await eda.pcb_Net.getAllNetsName();
      const pours=await eda.pcb_PrimitivePour.getAll();
      for(const item of p.layers){
        if(!nets.includes(item.net))throw Error('Unknown net');
        if(!liveLayers.some(l=>l.id===item.layer&&l.type==='SIGNAL'))throw Error('Native positive pours require a signal copper layer');
        if(item.pour_id){
          const old=pours.find(q=>q.getState_PrimitiveId()===item.pour_id);
          if(!old||old.getState_Layer()!==item.layer||old.getState_Net()!==item.net)throw Error('Refresh ID/layer/net mismatch');
        }else if(pours.some(q=>q.getState_Layer()===item.layer))throw Error('Existing pours on create layer; use explicit refresh IDs');
      }
      const job={job_key:p.job_key,document_uuid:p.source.document_uuid,status:'pending',
        created:[],refreshed:[],completed:[],errors:[],saved:false,success:false,applied:false,
        full_board_accepted:false,drc_requested:p.drc};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Status storage failed');};
      if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':backup',{source:original,request:p})!==true)throw Error('Backup storage failed');
      await persist();
      let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          try{
            await activeDocument(eda,p.source.document_uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before mutation');
            for(const item of p.layers){
              await activeDocument(eda,p.source.document_uuid);
              let pour;
              if(item.pour_id){
                pour=await eda.pcb_PrimitivePour.get(item.pour_id);job.refreshed.push(item.pour_id);
              }else{
                const polygon=eda.pcb_MathPolygon.createPolygon([...item.points[0],'L',...item.points.slice(1).flat(),...item.points[0]]);
                if(!polygon)throw Error('Polygon creation failed');
                pour=await eda.pcb_PrimitivePour.create(item.net,item.layer,polygon,'solid',false,item.name,1,1,false);
                if(!pour)throw Error('Pour creation failed');
                job.created.push(pour.getState_PrimitiveId());
              }
              const record={layer:item.layer,net:item.net,pour_id:pour.getState_PrimitiveId()};
              job.status='rebuilding';job.current=record;await persist();
              const rebuilt=!!(await pour.rebuildCopperRegion());
              const [readback]=await readCopper([record.pour_id]);
              const solid=readback.regions.filter(r=>r.fill&&r.path.some(path=>path.length>2));
              job.completed.push({...record,rebuilt,solid_regions:solid.length,empty:solid.length===0});await persist();
            }
            job.applied=true;
            await activeDocument(eda,p.source.document_uuid);
            if(p.save){job.saved=await eda.pcb_Document.save();if(job.saved!==true)throw Error('Applied but save failed; do not replay');}
            const copper=await readCopper(job.completed.map(c=>c.pour_id));
            job.empty_pours=copper.filter(c=>!c.regions.some(r=>r.fill&&r.path.some(path=>path.length>2))).map(c=>c.id);
            const coverage=new Map();
            for(const c of copper){
              const key=JSON.stringify([c.layer,c.net]);
              const entry=coverage.get(key)||{layer:c.layer,net:c.net,solid_regions:0};
              entry.solid_regions+=c.regions.filter(r=>r.fill&&r.path.some(path=>path.length>2)).length;
              coverage.set(key,entry);
            }
            job.coverage=[...coverage.values()];
            job.all_requested_layer_nets_have_copper=job.coverage.every(c=>c.solid_regions>0);
            const groundCoverage=job.coverage.filter(c=>p.ground_nets.includes(c.net));
            job.ground_layers_have_copper=groundCoverage.length>0&&groundCoverage.every(c=>c.solid_regions>0);
            if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':copper',copper)!==true)throw Error('Copper evidence storage failed');
            job.result_source={document_uuid:p.source.document_uuid,fingerprint:sourceFingerprint(await readSource(eda))};
            if(p.drc){
              job.status='checking_drc';await persist();
              const issues=await eda.pcb_Drc.check(true,false,true);
              if(!Array.isArray(issues))throw Error('Verbose DRC must return an array');
              await activeDocument(eda,p.source.document_uuid);
              const stable=sourceFingerprint(await readSource(eda))===job.result_source.fingerprint;
              if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':drc',{issues,strict:true,verbose:true,source:job.result_source,source_stable:stable})!==true)throw Error('DRC evidence storage failed');
              job.drc={passed:stable&&issues.length===0,source_stable:stable,issues_key:p.job_key+':drc'};
              if(!stable)throw Error('PCB source changed during DRC; inspect current state, do not replay');
            }
            try{job.zoomed=await eda.pcb_Document.zoomToBoardOutline();}catch(error){job.display_error=String(error);}
            job.success=true;
          }catch(error){job.errors.push(String(error));}
          job.status='final';await persist();
        })().catch(()=>{});
      });
      if(accepted!==true){job.status='final';job.errors.push('Timer rejected');await persist();}
      return job;
    })()'''.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False, allow_nan=False))


def apply_plan(bridge, plan, window_id, output_dir, save=False, run_drc=True):
    validate_plan(plan)
    if type(save) is not bool or type(run_drc) is not bool:
        raise ValueError("save and run_drc must be booleans")
    payload = dict(deepcopy(plan), job_key="pcb_native_copper:" + str(uuid4()), save=save, drc=run_drc)
    return dispatch(bridge, payload, apply_expression(payload), window_id, output_dir)


def dispatch(bridge, payload, expression, window_id, output_dir):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write(directory / "request.json", payload)
    before = bridge.execute(bridge._adapter_code("(async()=>{await activeDocument(eda," + json.dumps(payload["source"]["document_uuid"]) + ");const raw=await readSource(eda);return {raw,fingerprint:sourceFingerprint(raw)};})()"), window_id)
    (directory / "before-native.epru").write_text(before["raw"], encoding="utf-8")
    if before["fingerprint"] != payload["source"]["fingerprint"]:
        raise ValueError("Source changed; no job dispatched")
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    write(directory / "dispatch.json", result)
    return result


def prepare_widths(inspection, policy):
    source = source_identity(inspection.get("source"))
    if set(policy) - {"default_width", "net_widths"}:
        raise ValueError("Width policy only accepts default_width and explicit net_widths")
    default = policy.get("default_width", 10)
    overrides = policy.get("net_widths", {})
    if not isinstance(overrides, dict) or set(overrides) - set(inspection.get("nets", [])):
        raise ValueError("net_widths must use real network names")
    for width in [default, *overrides.values()]:
        if isinstance(width, bool) or not isinstance(width, (float, int)) or not math.isfinite(width) or width <= 0:
            raise ValueError("Width targets must be finite positive mil values; the ordinary default is 10")
    tracks = inspection.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("Run native inspect again to collect native LINE and ARC inventory")
    changes, blocked, unassigned = [], [], []
    for track in tracks:
        if not track.get("net"):
            unassigned.append(track["id"]);continue
        target = max(default, overrides.get(track["net"], default))
        if track["width"] < target:
            change = {"before": deepcopy(track), "width": target}
            (blocked if track["locked"] else changes).append(change)
    return dict(schema_version=1, mode="native_minimum_widths", source=source,
                copper_layers=inspection["copper_layers"], policy={"default_width": default, "net_widths": overrides},
                changes=changes, blocked=blocked, unassigned_copper_ids=unassigned, full_board_accepted=False)


def apply_widths(bridge, plan, window_id, output_dir, save=False):
    source_identity(plan.get("source"))
    if plan.get("mode") != "native_minimum_widths" or plan.get("schema_version") != 1 or plan.get("blocked"):
        raise ValueError("Native width plan is invalid or has locked traces requiring explicit resolution")
    if not isinstance(plan.get("changes"), list) or type(save) is not bool:
        raise ValueError("Invalid width changes or save flag")
    seen = set()
    for item in plan["changes"]:
        before = item.get("before", {})
        if (set(item) != {"before", "width"} or before.get("kind") not in ("line", "arc")
                or not isinstance(before.get("id"), str) or before["id"] in seen or before.get("locked") is not False
                or before.get("layer") not in plan.get("copper_layers", []) or not before.get("net")
                or isinstance(item["width"], bool) or not isinstance(item["width"], (float, int))
                or not math.isfinite(item["width"]) or not (item["width"] > 0 and item["width"] > before.get("width", math.inf))):
            raise ValueError("Widths may only increase identified unlocked copper lines/arcs")
        seen.add(before["id"])
    payload = dict(deepcopy(plan), job_key="pcb_native_copper:" + str(uuid4()), save=save)
    expression = "(async()=>{" + READ_TRACKS_JS + r'''
      const tracksEquivalent=(a,b)=>{
        const lines=x=>x.filter(t=>t.kind==='line');
        const arcs=x=>x.filter(t=>t.kind==='arc').map(({id,...t})=>JSON.stringify(t)).sort();
        return sameTraceCoverage(lines(a),lines(b))&&JSON.stringify(arcs(a))===JSON.stringify(arcs(b));
      };
      const p=PAYLOAD;
      await activeDocument(eda,p.source.document_uuid);
      const original=await readSource(eda);
      if(sourceFingerprint(original)!==p.source.fingerprint)throw Error('Source changed');
      const layers=(await eda.pcb_Layer.getAllLayers()).filter(l=>l.layerStatus!==0&&(l.type==='SIGNAL'||l.type==='PLANE')).map(l=>l.id);
      if(JSON.stringify([...layers].sort((a,b)=>a-b))!==JSON.stringify([...p.copper_layers].sort((a,b)=>a-b)))throw Error('Copper layers changed');
      const live=await readTracks(layers);
      for(const item of p.changes){
        const current=live.find(t=>t.id===item.before.id&&t.kind===item.before.kind);
        if(JSON.stringify(current)!==JSON.stringify(item.before))throw Error('Native track differs from inspected source');
      }
      const expected=live.map(t=>({...t}));
      const job={job_key:p.job_key,document_uuid:p.source.document_uuid,status:'pending',success:false,saved:false,
        changed:[],already_satisfied:[],errors:[],full_board_accepted:false,requires_copper_rebuild:true};
      const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(p.job_key,job)!==true)throw Error('Status storage failed');};
      if(await eda.sys_Storage.setExtensionUserConfig(p.job_key+':backup',{source:original,request:p})!==true)throw Error('Backup storage failed');
      await persist();let invoked=false;
      const accepted=eda.sys_Timer.setTimeoutTimer(p.job_key,500,()=>{
        if(invoked)return;invoked=true;
        return (async()=>{
          try{
            await activeDocument(eda,p.source.document_uuid);
            if(sourceFingerprint(await readSource(eda))!==p.source.fingerprint)throw Error('Source changed before mutation');
            for(const item of p.changes){
              await activeDocument(eda,p.source.document_uuid);
              const api=item.before.kind==='line'?eda.pcb_PrimitiveLine:eda.pcb_PrimitiveArc;
              const current=await readTracks(layers);
              const before=current.find(t=>t.id===item.before.id&&t.kind===item.before.kind);
              const required={...item.before,width:item.width};
              const already=item.before.kind==='line'&&(!before||before.width===item.width)&&traceCoveredByTraces(required,current.filter(t=>t.kind==='line'));
              if(already){
                expected.find(t=>t.id===item.before.id&&t.kind===item.before.kind).width=item.width;
                if(!tracksEquivalent(expected,current))throw Error('Native copper differs outside the completed width changes');
                job.already_satisfied.push({id:item.before.id,width:item.width,proof:'complete same-net/layer/width line coverage'});
                await persist();continue;
              }
              if(!before||JSON.stringify(before)!==JSON.stringify(item.before))throw Error('Track changed before width edit');
              if(!await api.modify(item.before.id,{lineWidth:item.width}))throw Error('Native width modification failed');
              expected.find(t=>t.id===item.before.id&&t.kind===item.before.kind).width=item.width;
              const readback=await readTracks(layers);
              for(const pending of p.changes){
                if(pending.before.kind==='line'&&!readback.some(t=>t.kind==='line'&&t.id===pending.before.id)
                    &&traceCoveredByTraces({...pending.before,width:pending.width},readback.filter(t=>t.kind==='line'))){
                  expected.find(t=>t.id===pending.before.id&&t.kind==='line').width=pending.width;
                }
              }
              if(!tracksEquivalent(expected,readback))throw Error('Native width/geometry readback mismatch');
              job.changed.push({id:item.before.id,kind:item.before.kind,net:item.before.net,before:item.before.width,width:item.width,
                proof:item.before.kind==='line'?'bidirectional complete line coverage':'exact arc geometry'});
              await persist();
            }
            job.applied=true;
            await activeDocument(eda,p.source.document_uuid);
            if(p.save){job.saved=await eda.pcb_Document.save();if(job.saved!==true)throw Error('Widths applied but save failed; do not replay');}
            await activeDocument(eda,p.source.document_uuid);
            if(!tracksEquivalent(expected,await readTracks(layers)))throw Error('Saved native copper geometry differs from width target');
            job.result_source={document_uuid:p.source.document_uuid,fingerprint:sourceFingerprint(await readSource(eda))};
            job.success=true;
          }catch(error){job.errors.push(String(error));}
          job.status='final';await persist();
        })().catch(()=>{});
      });
      if(accepted!==true){job.status='final';job.errors.push('Timer rejected');await persist();}
      return job;
    })()'''.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return dispatch(bridge, payload, expression, window_id, output_dir)


def classify_drc(report, ground_nets):
    """Flatten the observed native issue tree; unknown connection types stay open."""
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        raise ValueError("Verbose native DRC report required")
    grounds = set(ground_nets)
    leaves, unknown = [], []

    def walk(nodes, path=()):
        for node in nodes:
            if not isinstance(node, dict):
                unknown.append({"path": list(path), "raw": node});continue
            location = path + (str(node.get("name", "")),)
            if isinstance(node.get("list"), list):
                walk(node["list"], location);continue
            explanation = node.get("explanation", {})
            data = explanation.get("errData", {}) if isinstance(explanation, dict) else {}
            param = explanation.get("param", {}) if isinstance(explanation, dict) else {}
            data = data if isinstance(data, dict) else {}
            param = param if isinstance(param, dict) else {}
            category = node.get("errorType", data.get("errorType"))
            net = node.get("net", data.get("net"))
            if not isinstance(net, str):
                net = next((name for name in reversed(path) if name in grounds), None)
            connection = (category in ("Connection Error", "No Connection") or param.get("type") == "ConnectError"
                          or any(group in ("Connection Error", "No Connection") for group in path))
            if not category:
                unknown.append({"path": list(path), "raw": node})
            elif not connection and re.search(r"connect|unrout|disconnected|连通|连接|断路", str(category), re.I):
                unknown.append({"path": list(path), "raw": node, "reason": "Unknown connection category"})
            refs = []
            for key in ("obj1", "obj2"):
                if isinstance(data.get(key), str) and data[key]:
                    refs.append({"id": data[key], "type": data.get(key + "Type")})
            objects = node.get("objs", [])
            if not isinstance(objects, list):
                unknown.append({"path": list(path), "raw": node, "reason": "Unknown object list"})
                objects = []
            for obj in objects:
                if isinstance(obj, str) and obj and obj not in {r["id"] for r in refs}:
                    refs.append({"id": obj, "type": node.get("errorObjType")})
            leaves.append({"index": len(leaves), "category": category, "net": net,
                           "connection": connection, "ground": net in grounds,
                           "objects": refs, "native_display_position": node.get("pos"), "raw": node})

    walk(report["issues"])
    ground = [item for item in leaves if item["connection"] and item["ground"]]
    unresolved_connections = [item for item in leaves if item["connection"] and (not item["net"] or not item["objects"])]
    checked = report.get("strict") is True and report.get("verbose") is True and report.get("source_stable") is True
    return dict(source=deepcopy(report.get("source")), ground_nets=list(ground_nets), issue_count=len(leaves),
                ground_connection_count=len(ground), ground_targets=ground, issues=leaves,
                unclassified=unknown, unresolved_connections=unresolved_connections,
                native_check_complete=checked,
                ground_connections_reported_clear=checked and not ground and not unknown and not unresolved_connections,
                native_drc_passed=checked and not report["issues"], full_board_accepted=False,
                next_step="Resolve only listed physical gaps, rebuild affected pours, rerun strict native DRC")


def read_drc(bridge, document_uuid, window_id, ground_nets, output_dir):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    expression = "(async()=>{await activeDocument(eda," + json.dumps(document_uuid) + ");" + r'''
      const document=await eda.dmt_SelectControl.getCurrentDocumentInfo();
      const before=sourceFingerprint(await readSource(eda));
      const issues=await eda.pcb_Drc.check(true,false,true);
      if(!Array.isArray(issues))throw Error('Verbose DRC must return an array');
      await activeDocument(eda,document.uuid);
      return {source:{document_uuid:document.uuid,fingerprint:before},issues,strict:true,verbose:true,
        source_stable:sourceFingerprint(await readSource(eda))===before};
    })()'''
    raw = bridge.execute(bridge._adapter_code(expression), window_id)
    write(directory / "native-drc.json", raw)
    classified = classify_drc(raw, ground_nets)
    refs = {obj["id"]: obj for target in classified["ground_targets"] for obj in target["objects"]}
    expression = "(async()=>{await activeDocument(eda," + json.dumps(document_uuid) + ");const refs=" + json.dumps(list(refs.values())) + ";" + r'''
      const result=[];
      for(const ref of refs){
        let p,geometry;
        try{
          if(/pad/i.test(ref.type||'')){
            p=await eda.pcb_PrimitivePad.get(ref.id);
            if(p)geometry={x:p.getState_X(),y:p.getState_Y(),layer:p.getState_Layer()};
          }else if(/via/i.test(ref.type||'')){
            p=await eda.pcb_PrimitiveVia.get(ref.id);
            if(p)geometry={x:p.getState_X(),y:p.getState_Y()};
          }else if(/line|track/i.test(ref.type||'')){
            p=await eda.pcb_PrimitiveLine.get(ref.id);
            if(p)geometry={sx:p.getState_StartX(),sy:p.getState_StartY(),ex:p.getState_EndX(),ey:p.getState_EndY(),layer:p.getState_Layer(),width:p.getState_LineWidth()};
          }
          result.push({...ref,resolved:!!geometry,net:p?p.getState_Net():null,units:'mil',coordinate_frame:'native_pcb_data_origin',geometry});
        }catch(error){result.push({...ref,resolved:false,error:String(error)});}
      }
      return {source_fingerprint:sourceFingerprint(await readSource(eda)),objects:result};
    })()'''
    resolved = bridge.execute(bridge._adapter_code(expression), window_id)
    classified["live_objects"] = resolved["objects"]
    classified["endpoint_source_stable"] = resolved["source_fingerprint"] == raw["source"]["fingerprint"]
    if not classified["endpoint_source_stable"]:
        classified["ground_connections_reported_clear"] = False
    write(directory / "ground-repair-targets.json", classified)
    return {key: value for key, value in classified.items() if key not in ("issues", "ground_targets", "live_objects", "unclassified", "unresolved_connections")}


def job_status(bridge, job_key, window_id, output_dir=None):
    if not re.fullmatch(r"pcb_native_copper:[0-9a-f-]{36}", job_key):
        raise ValueError("Invalid native copper job key")
    result = bridge.execute("return (await eda.sys_Storage.getExtensionUserConfig(" + json.dumps(job_key) + "))??null;", window_id)
    if output_dir and result:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        write(directory / "status.json", result)
        for suffix in ("copper", "drc"):
            value = bridge.execute("return (await eda.sys_Storage.getExtensionUserConfig(" + json.dumps(job_key + ":" + suffix) + "))??null;", window_id)
            if value is not None:
                write(directory / (suffix + ".json"), value)
    return result


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--window-id")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Read native copper without approximating curved paths")
    inspect.add_argument("--document-uuid", required=True)
    inspect.add_argument("--output-dir", required=True)
    prepare = sub.add_parser("prepare", help="Explicit layer/net specification; no inferred power rails")
    prepare.add_argument("--board", required=True, help="Real snapshot, or inspection.json for refresh only")
    prepare.add_argument("--spec", required=True)
    prepare.add_argument("--output", required=True)
    widths = sub.add_parser("prepare-widths", help="Raise named copper LINE/ARC widths using an explicit minimum policy")
    widths.add_argument("--inspection", required=True)
    widths.add_argument("--policy", required=True, help="JSON default_width (default 10) and net_widths mapping")
    widths.add_argument("--output", required=True)
    width_apply = sub.add_parser("apply-widths", help="Modify only native lineWidth, retaining all paths and wider tracks")
    width_apply.add_argument("--plan", required=True)
    width_apply.add_argument("--audit-dir", required=True)
    width_apply.add_argument("--save", action="store_true")
    apply = sub.add_parser("apply", help="Dispatch one native pour/rebuild job; DRC is reported, not a rollback gate")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--audit-dir", required=True)
    apply.add_argument("--save", action="store_true")
    apply.add_argument("--skip-drc", action="store_true", help="Only for an explicitly requested visual preview")
    status = sub.add_parser("status")
    status.add_argument("--job-key", required=True)
    status.add_argument("--output-dir")
    drc = sub.add_parser("drc", help="Read strict verbose native DRC and only the requested ground gap targets")
    drc.add_argument("--document-uuid", required=True)
    drc.add_argument("--ground-net", action="append", required=True)
    drc.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_plan(read(args.board), read(args.spec))
        write(args.output, result)
        print(json.dumps({"output": args.output, "pours": len(result["layers"]), "source": result["source"]}, ensure_ascii=False))
        return 0
    if args.command == "prepare-widths":
        result = prepare_widths(read(args.inspection), read(args.policy))
        write(args.output, result)
        print(json.dumps({"output": args.output, "changes": len(result["changes"]), "blocked": len(result["blocked"]), "source": result["source"]}, ensure_ascii=False))
        return 1 if result["blocked"] else 0
    bridge = EasyEDABridge(args.base_url)
    if args.command == "inspect":
        result = inspect_board(bridge, args.document_uuid, args.window_id, args.output_dir)
    elif args.command == "apply":
        result = apply_plan(bridge, read(args.plan), args.window_id, args.audit_dir, args.save, not args.skip_drc)
    elif args.command == "apply-widths":
        result = apply_widths(bridge, read(args.plan), args.window_id, args.audit_dir, args.save)
    elif args.command == "status":
        result = job_status(bridge, args.job_key, args.window_id, args.output_dir)
    else:
        result = read_drc(bridge, args.document_uuid, args.window_id, args.ground_net, args.output_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result is None or (result.get("status") == "final" and result.get("success") is not True) else 0


if __name__ == "__main__":
    raise SystemExit(main())
