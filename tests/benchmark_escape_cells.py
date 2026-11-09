"""可重复微基准：相同预算、真实完整网表、原几何与连通检查，不使用手工种子。

直接运行输出 JSON；合成小板仅证明功能与回归边界，不代表工业板布通率。
"""
import json
import math
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcb_functional_cells import check_relations,validate_cells
from pcb_pipeline_common import check_board,normalize_board
from pcb_placement import place_components
from pcb_routing import route_board,check_routed_nets
from test_functional_cells import fixture as placement_fixture, options as placement_options, cells
from test_pcb_escape import fixture as escape_fixture, options as escape_options


def benchmark(repeats=3):
    """返回旧/新同预算的实测指标；异常传播，不能把失败样本改记成功。"""
    rows=[]
    for stage in ("placement","routing"):
        for mode in ("legacy","opt_in"):
            samples=[]
            for _ in range(repeats):
                if stage=="placement":
                    board=placement_fixture(); cfg=placement_options()
                    if mode=="legacy":cfg["functional_cells"]=[]
                    started=time.perf_counter()
                    result,stats=place_components(board,cfg)
                    metrics=dict(geometry_errors=len(check_board(result,ignore_connectivity=True)),
                        relation_errors=len(check_relations(result,validate_cells(cells(),result))),
                        hpwl_mil=stats["final_hpwl"],candidate_checks=stats["candidate_checks"],
                        supplied_absolute_seed=False)
                else:
                    board=escape_fixture(); cfg=escape_options()
                    # 为每个显式外围焊盘增加实际外部终点，两种模式均须接通完整 4 网。
                    for i in range(4):
                        x,y=(-120 if i<2 else 120),(-20 if i%2==0 else 20)
                        cid="P"+str(i);net="N"+str(i)
                        board["components"].append(dict(id=cid,x=x,y=y,rotation=0,layer=1,width=12,height=12,
                            pads=[dict(padId="1",net=net,localX=0,localY=0,shape="CIRCLE",diameter=12)]))
                        board["nets"][net].append(dict(compId=cid,padId="1"))
                    board=normalize_board(board)
                    cfg["escape"]["enabled"]=mode=="opt_in"
                    started=time.perf_counter()
                    result,stats=route_board(board,cfg)
                    metrics=dict(geometry_errors=len(check_board(result,ignore_connectivity=True)),
                        connectivity_errors=len(check_routed_nets(result,list(board["nets"]))),
                        declared_nets=len(board["nets"]),wirelength_mil=sum(math.hypot(t["ex"]-t["sx"],t["ey"]-t["sy"]) for t in result["routes"]["traces"]),
                        vias=len(result["routes"].get("vias",[])),escape_outlets=len(stats.get("escape",{}).get("escaped",[])),
                        expansions=stats.get("expansions",0))
                metrics["elapsed_seconds"]=time.perf_counter()-started
                samples.append(metrics)
            stable=[{k:v for k,v in sample.items() if k!="elapsed_seconds"} for sample in samples]
            rows.append(dict(stage=stage,mode=mode,repeats=repeats,
                deterministic_metrics=all(item==stable[0] for item in stable),metrics=stable[0],
                median_seconds=statistics.median(s["elapsed_seconds"] for s in samples)))
    return dict(scope="synthetic_functional_microbenchmark_not_industrial_routability",rows=rows,
                defaults_changed=False,global_optimum_proven=False)


if __name__=="__main__":
    print(json.dumps(benchmark(),ensure_ascii=False,indent=2,allow_nan=False))
