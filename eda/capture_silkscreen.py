"""只读采集指定 PCB 的属性、组件与独立线弧；不修改、不保存原生文档。"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pcb_bridge import EasyEDABridge


def main():
    """入参：窗口、基线指纹、输出目录；出参：同源文件；异常：来源变化或输出已存在立即停止。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-id", required=True)
    parser.add_argument("--document-uuid", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=False)
    for name in ("snapshot.json", "native-source.epru"):
        if (out / name).exists():
            raise FileExistsError(str(out / name))
    expression = r'''(async()=>{
      await activeDocument(eda,DOCUMENT_UUID);
      const raw=await readSource(eda);
      const fingerprint=sourceFingerprint(raw);
      if(fingerprint!==EXPECTED_FINGERPRINT)throw Error("Source changed before read-only silkscreen capture");
      function attr(a){return {id:a.getState_PrimitiveId(),parentId:a.getState_ParentPrimitiveId(),key:a.getState_Key(),value:a.getState_Value(),layer:a.getState_Layer(),x:a.getState_X(),y:a.getState_Y(),fontFamily:a.getState_FontFamily(),fontSize:a.getState_FontSize(),lineWidth:a.getState_LineWidth(),alignMode:a.getState_AlignMode(),rotation:a.getState_Rotation(),keyVisible:a.getState_KeyVisible(),valueVisible:a.getState_ValueVisible(),reverse:a.getState_Reverse(),expansion:a.getState_Expansion(),mirror:a.getState_Mirror(),locked:a.getState_PrimitiveLock()};}
      const attributes=(await eda.pcb_PrimitiveAttribute.getAll()).map(attr);
      const components=(await eda.pcb_PrimitiveComponent.getAll()).map(c=>({id:c.getState_PrimitiveId(),designator:c.getState_Designator(),footprint:c.getState_Footprint(),layer:c.getState_Layer(),x:c.getState_X(),y:c.getState_Y(),rotation:c.getState_Rotation(),locked:c.getState_PrimitiveLock()}));
      function line(a){return {id:a.getState_PrimitiveId(),layer:a.getState_Layer(),net:a.getState_Net(),startX:a.getState_StartX(),startY:a.getState_StartY(),endX:a.getState_EndX(),endY:a.getState_EndY(),lineWidth:a.getState_LineWidth(),locked:a.getState_PrimitiveLock()};}
      const lines=(await eda.pcb_PrimitiveLine.getAll()).map(line);
      const arcs=(await eda.pcb_PrimitiveArc.getAll()).map(a=>({...line(a),arcAngle:a.getState_ArcAngle(),interactiveMode:a.getState_InteractiveMode()}));
      const layers=await eda.pcb_Layer.getAllLayers();
      if(sourceFingerprint(await readSource(eda))!==fingerprint)throw Error("Source changed during read-only silkscreen capture");
      return {schema_version:1,read_only:true,source:{document_uuid:DOCUMENT_UUID,fingerprint},attributes,components,lines,arcs,layers,raw,unavailable_api_fields:["character_width","character_spacing","text_bounding_box"],units:{position:"mil",fontSize:"mil",lineWidth:"mil",rotation:"degree"}};
    })()'''
    expression = expression.replace("DOCUMENT_UUID", json.dumps(args.document_uuid))
    bridge = EasyEDABridge(timeout=60)
    expression = expression.replace("EXPECTED_FINGERPRINT", json.dumps(args.fingerprint))
    result = bridge.execute(bridge._adapter_code(expression), args.window_id)
    raw = result.pop("raw")
    (out / "native-source.epru").write_text(raw, encoding="utf-8")
    (out / "snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"source": result["source"], "counts": {k: len(result[k]) for k in ("attributes", "components", "lines", "arcs")}, "output": str(out / "snapshot.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
