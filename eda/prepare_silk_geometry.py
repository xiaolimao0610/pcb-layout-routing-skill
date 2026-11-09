"""将当前工程捕获的封装顶丝印按实时器件姿态转为世界坐标避让障碍。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shapely.geometry import LineString, Polygon, mapping

TOLERANCE = 0.05
MAX_SAMPLES = 20000


def records(path):
    """入参：原生文本路径；返回头/载荷列表；格式错误抛出解析异常。"""
    return [(json.loads(a), json.loads(b)) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() for a, b in [line.rstrip("|").split("||", 1)]]


def transformed(point, component):
    """入参：封装局部点、顶层器件；返回 mil 世界坐标；底层须另证镜像，因此拒绝。"""
    if component["layer"] != 1:
        raise ValueError("当前世界变换只对已采集的顶层封装验证，不能推断底层镜像")
    angle = math.radians(component["rotation"])
    x, y = point
    return [component["x"] + x * math.cos(angle) - y * math.sin(angle),
            component["y"] + x * math.sin(angle) + y * math.cos(angle)]


def sample_path(path, component):
    """入参：官方 L/ARC/CIRCLE 局部路径与姿态；返回近似点及精确世界线弧定义。

    曲线最大弦误差为 0.05 mil，单路径最多 20000 点；未知命令或预算耗尽抛异常。
    """
    if path[0] == "CIRCLE":
        _, x, y, radius, *direction = path
        if radius <= 0:
            raise ValueError("圆半径必须为正")
        count = max(16, math.ceil(2 * math.pi / (2 * math.acos(max(-1, 1-TOLERANCE/radius)))))
        if count + 1 > MAX_SAMPLES:
            raise ValueError("丝印圆采样预算耗尽")
        local = [[x+radius*math.cos(2*math.pi*i/count), y+radius*math.sin(2*math.pi*i/count)] for i in range(count+1)]
        return [transformed(p, component) for p in local], [{"kind":"circle", "center":transformed([x,y],component), "radius":radius}]
    current = list(path[:2])
    points, segments, i, mode = [transformed(current, component)], [], 2, None
    while i < len(path):
        if isinstance(path[i], str):
            mode = path[i]
            i += 1
            if mode not in ("L", "ARC", "CARC"):
                raise ValueError("未支持的真实丝印路径命令：" + mode)
        if mode == "L":
            end = list(path[i:i+2]); i += 2
            segments.append({"kind":"line", "start":transformed(current,component), "end":transformed(end,component)})
            points.append(transformed(end,component)); current=end
        elif mode in ("ARC", "CARC"):
            degrees, ex, ey = path[i:i+3]; i += 3
            end=[ex,ey]; theta=math.radians(degrees)
            dx,dy=ex-current[0],ey-current[1]; chord=math.hypot(dx,dy)
            if not 0 < abs(degrees) < 360 or chord == 0:
                raise ValueError("丝印弧端点/角度退化")
            radius=chord/(2*math.sin(abs(theta)/2))
            height=chord/(2*math.tan(theta/2))
            center=[current[0]+dx/2-dy/chord*height,current[1]+dy/2+dx/chord*height]
            start=math.atan2(current[1]-center[1],current[0]-center[0])
            step=2*math.acos(max(-1,1-TOLERANCE/radius))
            count=max(1,math.ceil(abs(theta)/step))
            if count+len(points)>MAX_SAMPLES:
                raise ValueError("丝印弧采样预算耗尽")
            points.extend(transformed([center[0]+radius*math.cos(start+theta*j/count),center[1]+radius*math.sin(start+theta*j/count)],component) for j in range(1,count))
            points.append(transformed(end,component))
            segments.append({"kind":"arc", "start":transformed(current,component), "end":transformed(end,component), "center":transformed(center,component), "radius":radius, "angle":degrees})
            current=end; mode=None
        else:
            raise ValueError("路径缺少明确命令")
        if len(points)>MAX_SAMPLES:
            raise ValueError("丝印路径采样预算耗尽")
    return points, segments


def main():
    """入参：本次只读捕获目录及封装捕获目录；输出新 geometry.json；错误拒绝覆盖/继续。"""
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture-dir", required=True)
    ap.add_argument("--footprint-capture", required=True)
    ap.add_argument("--output", required=True)
    args=ap.parse_args()
    capture, library, output=Path(args.capture_dir),Path(args.footprint_capture),Path(args.output)
    if output.exists(): raise FileExistsError(output)
    snapshot=json.loads((capture/"snapshot.json").read_text(encoding="utf-8"))
    receipt=json.loads((library/"context-receipt.json").read_text(encoding="utf-8"))
    footprints={f["footprint_key"]:f for f in receipt["footprints"]}
    raw_rows=records(capture/"native-source.epru")
    attrs={h["id"]:p for h,p in raw_rows if h["type"]=="ATTR"}
    result={"schema_version":1,"source":snapshot["source"],"units":"mil","curve_chord_tolerance_mil":TOLERANCE,
            "method":"当前工程已捕获封装源码 + 本次实时顶层器件姿态；原始局部路径和精确世界线弧保留；障碍按笔宽及曲线误差保守膨胀",
            "attributes":[],"obstacles":[],"unsupported":[]}
    for header,payload in raw_rows:
        if payload.get("layerId")==3 and header["type"] not in ("ATTR","LAYER"):
            result["unsupported"].append({"owner":None,"native_id":header.get("id"),"type":header["type"],"raw":payload,
                                          "reason":"独立顶丝印不是已验证的封装轮廓；必须补充障碍后才能声称完整避让"})
    for attr in snapshot["attributes"]:
        raw=attrs.get(attr["id"],{})
        result["attributes"].append({**attr,"native_origin":raw.get("origin"),"native_width":raw.get("width"),"native_height":raw.get("height"),"native_path":raw.get("path"),
                                     "native_present_fields":[k for k in ("width","height","path") if k in raw],"native_raw":raw if raw else None,
                                     "native_record_present":attr["id"] in attrs})
    parsed={}
    for component in snapshot["components"]:
        ref=component["footprint"];key=ref["libraryUuid"]+":"+ref["uuid"]
        fp=footprints[key];file=library/fp["source_file"]
        if key not in parsed:
            text=file.read_text(encoding="utf-8")
            if "sha256:"+hashlib.sha256(text.encode("utf-8")).hexdigest()!=fp["source_sha256"]:
                raise ValueError("封装源码摘要与已捕获证据不符："+key)
            parsed[key]=records(file)
        for header,payload in parsed[key]:
            if payload.get("layerId")!=3: continue
            kind=header["type"]
            if kind=="ATTR": continue
            if kind not in ("POLY","FILL"):
                result["unsupported"].append({"owner":component["id"],"local_id":header.get("id"),"type":kind,"raw":payload}); continue
            width=float(payload["width"])
            paths=payload["path"] if kind=="FILL" else [payload["path"]]
            samples=[];segments=[]
            for path in paths:
                pts,parts=sample_path(path,component);samples.append(pts);segments.extend(parts)
            if kind=="FILL":
                if payload.get("fillStyle")!="SOLID" or len(samples)!=1: raise ValueError("未验证的丝印填充样式/多环")
                shape=Polygon(samples[0]);distance=(width/2+TOLERANCE)/math.cos(math.pi/64)
            else:
                shape=LineString(samples[0]);distance=(width/2+TOLERANCE)/math.cos(math.pi/64)
            if shape.is_empty or not shape.is_valid: raise ValueError("丝印几何无效")
            shape=shape.buffer(distance,quad_segs=16)
            result["obstacles"].append({"id":component["id"]+":"+header["id"],"owner":component["id"],"designator":component["designator"],"local_id":header["id"],"layer":3,"kind":kind,
                "lineWidth":width,"local_path":payload["path"],"world_segments":segments,"geometry":mapping(shape),"footprint_key":key,"source_file":str(file),"source_sha256":fp["source_sha256"],
                "immutable_reason":"封装轮廓及潜在极性标识均保留；不分类猜测并删除"})
    result["summary"]={"attributes":len(result["attributes"]),"designators":sum(a["key"].lower()=="designator" for a in result["attributes"]),"obstacles":len(result["obstacles"]),"unsupported":len(result["unsupported"])}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result["summary"],ensure_ascii=False))


if __name__=="__main__":main()
