"""逐铜层渲染真实几何及前后对比；图像仅供检查，不替代独立验收。"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import struct
import time
import zlib

import numpy as np
from shapely import contains_xy
from shapely.errors import GEOSException
from shapely.ops import unary_union

from pcb_pipeline_common import (PipelineError, board_polygon, copper_and_drills,
                                 finite, normalize_board, rules_for, strict_options)
from pcb_solver_hardened import _body


_OPTIONS = {"layers", "width", "height", "margin_pixels", "max_pixels", "max_seconds",
            "geometry", "show_components"}
_COLORS = {"background": (250, 250, 248), "board": (232, 235, 230),
           "outline": (51, 61, 70), "component": (115, 121, 132),
           "pad": (232, 172, 34), "via": (163, 105, 36), "hole": (255, 255, 255),
           "added": (17, 154, 87), "removed": (218, 53, 86),
           "net_changed": (152, 68, 185), "unchanged": (168, 176, 181)}
_LAYER_COLORS = ((206, 52, 58), (40, 95, 186), (37, 139, 111),
                 (135, 80, 177), (180, 108, 24), (20, 132, 159))
_HEADER = 24
# 仅使用固定英文状态和数字标记，详细中文图例由清单及HTML提供。
_FONT = {
    "A": (14,17,17,31,17,17,17), "B": (30,17,17,30,17,17,30),
    "D": (30,17,17,17,17,17,30), "E": (31,16,16,30,16,16,31),
    "F": (31,16,16,30,16,16,16), "I": (31,4,4,4,4,4,31),
    "L": (16,16,16,16,16,16,31), "O": (14,17,17,17,17,17,14),
    "R": (30,17,17,30,20,18,17), "T": (31,4,4,4,4,4,4),
    "Y": (17,17,10,4,4,4,4), " ": (0,0,0,0,0,0,0),
    "0": (14,17,19,21,25,17,14), "1": (4,12,4,4,4,4,14),
    "2": (14,17,1,2,4,8,31), "3": (30,1,1,14,1,1,30),
    "4": (2,6,10,18,31,2,2), "5": (31,16,16,30,1,1,30),
    "6": (14,16,16,30,17,17,14), "7": (31,1,2,4,8,8,8),
    "8": (14,17,17,14,17,17,14), "9": (14,17,17,15,1,1,14),
}


class RenderFailure(PipelineError):
    """带预算和未完成输出证据的渲染失败，不报告已检查或验收通过。"""
    def __init__(self, message, **diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def _settings(board, options):
    """输入板及选项，返回严格配置；未知字段、层号或预算非法抛PipelineError。"""
    result = strict_options(options, _OPTIONS, "render")
    for key, default, low, high in (("width", 1200, 128, 4096), ("height", 900, 128, 4096),
                                    ("margin_pixels", 24, 0, 256),
                                    ("max_pixels", 60000000, 1, 268435456)):
        value = result.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise PipelineError(f"render.{key} 必须为{low}..{high}的整数")
        result[key] = value
    if result["margin_pixels"]*2 + _HEADER >= min(result["width"], result["height"]):
        raise PipelineError("render.margin_pixels 没有留下有效绘图区")
    result["max_seconds"] = finite(result.get("max_seconds", 60.0), "render.max_seconds", positive=True)
    result.setdefault("geometry", "inner")
    if result["geometry"] not in ("inner", "outer"):
        raise PipelineError("render.geometry 只能为inner或outer")
    result.setdefault("show_components", True)
    if type(result["show_components"]) is not bool:
        raise PipelineError("render.show_components 必须是布尔值")
    stack = list(rules_for(board).copper_layers)
    layers = result.get("layers", stack)
    if (not isinstance(layers, list) or not layers or any(type(v) is not int for v in layers)
            or len(set(layers)) != len(layers) or set(layers)-set(stack)):
        raise PipelineError("render.layers 必须为不重复的实际铜层列表")
    result["layers"] = [layer for layer in stack if layer in layers]
    return result


class _Budget:
    """合作式总时限覆盖几何、光栅化和写图，不抢占单次库调用。"""
    def __init__(self, seconds, started):
        self.seconds, self.started = seconds, started

    def check(self):
        if time.monotonic()-self.started >= self.seconds:
            raise RenderFailure("渲染时间预算耗尽，未完成全部图像", code="RENDER_TIME_BUDGET")


def _geometry(board, settings, budget):
    """从原几何模型提取每层实体，返回铜、孔与器件轮廓；几何错误原样失败。"""
    budget.check()
    copper, drills = copper_and_drills(board)
    budget.check()
    bodies = []
    if settings["show_components"]:
        tolerance = rules_for(board).curve_tolerance
        for component in board["components"]:
            budget.check()
            bodies.append((component["layer"], _body(component, tolerance).outer, component["id"]))
    return dict(board=board_polygon(board), copper=copper, drills=drills, bodies=bodies)


def _viewport(geometries, settings):
    """计算所有前后可见对象共用的等比例视口，返回可复算的mil到像素变换。"""
    bounds = []
    for geo in geometries:
        bounds.append(geo["board"].bounds)
        bounds.extend(item["outer"].bounds for kind in ("copper", "drills") for item in geo[kind]
                      if not item["outer"].is_empty)
        bounds.extend(shape.bounds for _, shape, _ in geo["bodies"] if not shape.is_empty)
    x0, y0 = min(b[0] for b in bounds), min(b[1] for b in bounds)
    x1, y1 = max(b[2] for b in bounds), max(b[3] for b in bounds)
    width, height, margin = settings["width"], settings["height"], settings["margin_pixels"]
    scale = min((width-2*margin)/(x1-x0), (height-_HEADER-2*margin)/(y1-y0))
    if not math.isfinite(scale) or scale <= 0:
        raise PipelineError("渲染视口坐标不能形成有限比例")
    ox = (width-(x1-x0)*scale)/2-x0*scale
    oy = _HEADER+(height-_HEADER-(y1-y0)*scale)/2+y1*scale
    return dict(width=width, height=height, content_bounds_mil=[x0,y0,x1,y1],
                pixels_per_mil=scale, x_offset_pixels=ox, y_offset_pixels=oy,
                transform="pixel_x=x*scale+x_offset; pixel_y=-y*scale+y_offset",
                pixel_sampling="像素中心采样，不膨胀微小图元；小于一像素的细节可能不可见",
                y_axis="世界y向上，图像y向下；所有层均为同一顶视坐标，不单独镜像底层")


def _mask(shape, view, budget):
    """将真实几何按像素中心批量采样为布尔图；分块检查时间预算。"""
    width, height = view["width"], view["height"]
    result = np.zeros((height, width), dtype=np.bool_)
    if shape.is_empty:
        return result
    scale, ox, oy = view["pixels_per_mil"], view["x_offset_pixels"], view["y_offset_pixels"]
    x0,y0,x1,y1 = shape.bounds
    left, right = max(0, math.floor(x0*scale+ox)), min(width, math.ceil(x1*scale+ox))
    top, bottom = max(_HEADER, math.floor(-y1*scale+oy)), min(height, math.ceil(-y0*scale+oy))
    if left >= right or top >= bottom:
        return result
    xs = (np.arange(left, right, dtype=float)+0.5-ox)/scale
    for start in range(top, bottom, 128):
        budget.check()
        stop = min(bottom, start+128)
        ys = (oy-np.arange(start, stop, dtype=float)-0.5)/scale
        result[start:stop,left:right] = contains_xy(shape, xs[None,:], ys[:,None])
    return result


def _title(pixels, text):
    """绘制固定小字标题；不依赖系统字体，结果跨平台可复现。"""
    pixels[:_HEADER] = (255,255,255)
    for i, char in enumerate(text):
        for y, row in enumerate(_FONT[char]):
            for x in range(5):
                if row & (1 << (4-x)):
                    px,py = 10+i*12+x*2, 5+y*2
                    if px+2 <= pixels.shape[1]:
                        pixels[py:py+2,px:px+2] = _COLORS["outline"]


def _union(shapes, budget):
    """合并当前层同类实体，空输入返回空几何；库调用前后检查预算。"""
    budget.check()
    result = unary_union(shapes)
    budget.check()
    return result


def _layer_shapes(geometry, layer, setting, budget):
    """返回原模型扣孔后的分类型铜和完整钻孔，几何内外界显式可选。"""
    kinds = {key: [] for key in ("pour", "trace", "via", "pad")}
    by_net = {}
    for item in geometry["copper"]:
        if layer not in item["layers"]:
            continue
        shape = item[setting]
        kinds[item["name"].split(" ",1)[0]].append(shape)
        by_net.setdefault(item["net"], []).append(shape)
    result = {key:_union(shapes,budget) for key,shapes in kinds.items()}
    result["by_net"] = {net:_union(shapes,budget) for net,shapes in by_net.items()}
    result["copper"] = _union(list(result["by_net"].values()),budget)
    # 为免显示假铜，inner模式取钻孔outer，outer模式取钻孔inner，与原模型一致。
    hole_bound = "outer" if setting == "inner" else "inner"
    result["holes"] = _union([d[hole_bound] for d in geometry["drills"] if layer in d["layers"]],budget)
    return result


def _base(geometry, layer, view, budget):
    pixels = np.empty((view["height"],view["width"],3),dtype=np.uint8)
    pixels[:] = _COLORS["background"]
    pixels[_mask(geometry["board"],view,budget)] = _COLORS["board"]
    stroke = 0.65/view["pixels_per_mil"]
    pixels[_mask(geometry["board"].boundary.buffer(stroke),view,budget)] = _COLORS["outline"]
    return pixels


def _draw_layer(geometry, shapes, layer, color, view, budget, title):
    """叠放铺铜、线路、过孔和焊盘，最后显式扣孔，返回RGB数组。"""
    pixels = _base(geometry,layer,view,budget)
    pale = tuple(round(c*0.45+255*0.55) for c in color)
    for key, rgb in (("pour",pale),("trace",color),("via",_COLORS["via"]),("pad",_COLORS["pad"])):
        pixels[_mask(shapes[key],view,budget)] = rgb
    stroke = 0.65/view["pixels_per_mil"]
    for installed_layer, body, _ in geometry["bodies"]:
        if installed_layer == layer:
            pixels[_mask(body.boundary.buffer(stroke),view,budget)] = _COLORS["component"]
    pixels[_mask(shapes["holes"],view,budget)] = _COLORS["hole"]
    _title(pixels,title)
    return pixels


def _difference(before, after, geometry, layer, view, budget):
    """按网络比较真实几何增删，网络重分配单独标色，不仅比较铜总面积。"""
    added, removed = [], []
    nets = sorted(set(before["by_net"]) | set(after["by_net"]))
    empty = unary_union([])
    for net in nets:
        budget.check()
        old,new = before["by_net"].get(net,empty),after["by_net"].get(net,empty)
        added.append(new.difference(old))
        removed.append(old.difference(new))
    added,removed = _union(added,budget),_union(removed,budget)
    renamed = added.intersection(removed)
    pixels = _base(geometry,layer,view,budget)
    pixels[_mask(before["copper"].intersection(after["copper"]),view,budget)] = _COLORS["unchanged"]
    pixels[_mask(removed,view,budget)] = _COLORS["removed"]
    pixels[_mask(added,view,budget)] = _COLORS["added"]
    pixels[_mask(renamed,view,budget)] = _COLORS["net_changed"]
    _title(pixels,f"DIFF LAYER {layer}")
    return pixels,dict(added_copper_area_mil2=added.area,removed_copper_area_mil2=removed.area,
                       reassigned_net_area_mil2=renamed.area,
                       holes_added_area_mil2=after["holes"].difference(before["holes"]).area,
                       holes_removed_area_mil2=before["holes"].difference(after["holes"]).area)


def _write_png(path, pixels, budget):
    """将RGB uint8数组写为标准无损PNG；仅使用stdlib，写入异常向调用方传播。"""
    budget.check()
    height,width,channels = pixels.shape
    if pixels.dtype != np.uint8 or channels != 3:
        raise PipelineError("PNG输入必须为RGB uint8数组")
    def chunk(kind, data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)
    compressor = zlib.compressobj(level=6)
    blocks = []
    for start in range(0,height,128):
        budget.check()
        batch = pixels[start:start+128]
        rows = np.empty((len(batch),width*3+1),dtype=np.uint8)
        rows[:,0] = 0
        rows[:,1:] = batch.reshape((len(batch),width*3))
        blocks.append(compressor.compress(rows.tobytes()))
    blocks.append(compressor.flush())
    payload = (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))
               +chunk(b"IDAT",b"".join(blocks))+chunk(b"IEND",b""))
    budget.check()
    with path.open("xb") as stream:
        stream.write(payload)
    return dict(path=path.name,width=width,height=height,sha256=hashlib.sha256(payload).hexdigest())


def _write_manifest(path, manifest):
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def render_board(board, output_dir, options=None, *, before=None):
    """导出逐层PNG和同尺度前后对照，返回带来源/图例/坐标变换的清单。

    入参：完整mil板、必须不存在的输出目录、显式渲染选项及可选前快照。
    出参：manifest字典并写manifest.json/index.html；不修改输入、来源或工程。
    两快照须具有相同物理层栈。像素中心采样会丢失亚像素细节，默认使用
    保守铜内界并扣钻孔外界；几何外界模式另行声明，均不构成DRC或回流验收。
    异常：非法输入抛PipelineError；时间/像素预算抛RenderFailure；文件错误
    按OSError传播。创建目录后的失败保留incomplete清单，不隐藏半成品或重写旧图。
    """
    started = time.monotonic()
    original = normalize_board(board)
    settings = _settings(original,options)
    budget = _Budget(settings["max_seconds"],started)
    budget.check()
    earlier = normalize_board(before) if before is not None else None
    stack = list(rules_for(original).copper_layers)
    if earlier is not None and list(rules_for(earlier).copper_layers) != stack:
        raise PipelineError("前后对比必须使用相同物理铜层栈，不能猜测层映射")
    count = len(settings["layers"])*(5 if earlier is not None else 1)
    total = count*settings["width"]*settings["height"]
    if total > settings["max_pixels"]:
        raise RenderFailure("渲染总像素预算不足，未生成图像",code="RENDER_PIXEL_BUDGET",requested_pixels=total,
                            max_pixels=settings["max_pixels"])
    directory = Path(output_dir).resolve()
    if directory.exists():
        raise PipelineError("渲染输出目录已存在，请选择全新目录")
    if not directory.parent.is_dir():
        raise PipelineError("渲染输出目录的父目录不存在")
    after_geo = _geometry(original,settings,budget)
    before_geo = _geometry(earlier,settings,budget) if earlier is not None else None
    view = _viewport([g for g in (before_geo,after_geo) if g is not None],settings)
    budget.check()
    manifest = dict(schema_version=1,status="incomplete",render_complete=False,full_board_accepted=False,
                    source=original.get("metadata",{}).get("source"),
                    before_source=earlier.get("metadata",{}).get("source") if earlier is not None else None,
                    options=settings,viewport=view,total_planned_pixels=total,files=[],layers=[],
                    legend={key:list(value) for key,value in _COLORS.items()},
                    comparison_columns=["before","after","difference"] if earlier is not None else [],
                    geometry_note="直接读取原铜/孔模型的inner或outer实体，不用中心线代替铜，不填平孔或板内切孔。",
                    acceptance_note="已渲染不等于已查看；清单不宣称AI视觉审查、独立DRC或实板验收完成。")
    directory.mkdir(exist_ok=False)
    try:
        _write_manifest(directory/"manifest.json",manifest)
        for layer in settings["layers"]:
            budget.check()
            color = _LAYER_COLORS[stack.index(layer)%len(_LAYER_COLORS)]
            after_shapes = _layer_shapes(after_geo,layer,settings["geometry"],budget)
            after_image = _draw_layer(after_geo,after_shapes,layer,color,view,budget,f"AFTER LAYER {layer}")
            row = dict(layer=layer,copper_color=list(color),after=f"layer-{layer}-after.png",
                       copper_area_mil2=after_shapes["copper"].area,
                       hole_area_mil2=after_shapes["holes"].area,
                       copper_objects=sum(layer in item["layers"] for item in after_geo["copper"]),
                       drill_objects=sum(layer in item["layers"] for item in after_geo["drills"]))
            manifest["files"].append(_write_png(directory/row["after"],after_image,budget))
            if earlier is not None:
                before_shapes = _layer_shapes(before_geo,layer,settings["geometry"],budget)
                before_image = _draw_layer(before_geo,before_shapes,layer,color,view,budget,f"BEFORE LAYER {layer}")
                row.update(before=f"layer-{layer}-before.png",comparison=f"layer-{layer}-comparison.png")
                manifest["files"].append(_write_png(directory/row["before"],before_image,budget))
                diff_image,row["difference"] = _difference(before_shapes,after_shapes,after_geo,layer,view,budget)
                joined = np.concatenate((before_image,after_image,diff_image),axis=1)
                joined[:,[view["width"],view["width"]*2]] = _COLORS["outline"]
                manifest["files"].append(_write_png(directory/row["comparison"],joined,budget))
            manifest["layers"].append(row)
            _write_manifest(directory/"manifest.json",manifest)
        budget.check()
        legend = "铺铜为浅色，走线为该层深色；焊盘金色、过孔棕色、钻孔白色。差异图：绿色新增铜、红色删除铜、紫色网络重新分配、灰色未变铜。"
        panels = "".join(f'<section><h2>铜层{row["layer"]}</h2><img alt="铜层{row["layer"]}" src="{html.escape(row.get("comparison",row["after"]))}"></section>'
                         for row in manifest["layers"])
        page = ('<!doctype html><meta charset="utf-8"><title>PCB逐层视觉检查</title>'
                '<style>body{font:16px sans-serif;max-width:1600px;margin:24px auto;padding:0 16px;color:#223}img{max-width:100%;border:1px solid #ccd}section{margin:24px 0}</style>'
                f'<h1>PCB逐层视觉检查</h1><p>{legend}</p><p>对比图顺序：修改前、修改后、几何增删。各面板共用坐标和尺度；所有层均按同一顶视坐标显示。</p>'
                '<p>图像供人工或AI实际查看，不替代几何、连通、回流、原生DRC和制造验收。详细来源及采样参数见manifest.json。</p>'+panels)
        (directory/"index.html").write_text(page,encoding="utf-8")
        budget.check()
        manifest.update(status="rendered",render_complete=True,elapsed_seconds=time.monotonic()-started)
        _write_manifest(directory/"manifest.json",manifest)
        return manifest
    except (PipelineError,OSError,GEOSException,ValueError,TypeError,OverflowError) as exc:
        manifest.update(status="incomplete",render_complete=False,error=str(exc),elapsed_seconds=time.monotonic()-started)
        if hasattr(exc,"diagnostics"):
            manifest["diagnostics"] = exc.diagnostics
        _write_manifest(directory/"manifest.json",manifest)
        raise


def main(argv=None):
    """读取完整快照及可选前快照/配置后渲染；成功返回0，错误保留证据并返回1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board",type=Path,required=True)
    parser.add_argument("--before",type=Path)
    parser.add_argument("--options",type=Path)
    parser.add_argument("--output-dir",type=Path,required=True)
    args = parser.parse_args(argv)
    try:
        def read(path):
            return json.loads(path.read_text(encoding="utf-8-sig"))
        result = render_board(read(args.board),args.output_dir,read(args.options) if args.options else None,
                              before=read(args.before) if args.before else None)
        print(json.dumps({key:result[key] for key in ("status","render_complete","full_board_accepted","elapsed_seconds")},ensure_ascii=False))
        return 0
    except (PipelineError,OSError,GEOSException,ValueError,TypeError,KeyError,OverflowError) as exc:
        print(json.dumps(dict(status="failed",render_complete=False,error=str(exc),
                              diagnostics=getattr(exc,"diagnostics",{})),ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
