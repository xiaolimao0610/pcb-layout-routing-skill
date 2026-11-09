"""独立隔离审查参考铜锚链：合成几何，不连接原生、不包含工程源。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from shapely.geometry import Point,box,LineString,MultiPolygon
import pcb_ground_repair as repair
from pcb_pipeline_common import PipelineError


def fixture():
    """返回只包含待查关系的板、逐层铜及真实镀孔夹具。"""
    void=Point(50,50).buffer(2)
    ring=Point(50,50).buffer(4).difference(void)
    plane=box(0,0,100,100).difference(void)
    trace=LineString([(50,50),(150,50)]).buffer(2).difference(void)
    rows=[]
    def add(name,layer,geometry,terminal=None,barrel=None):
        rows.append(dict(name=name,net='REF',layers=(layer,),inner=geometry,outer=geometry,terminal=terminal,barrel=barrel))
    add('pour 0.0',15,plane)
    add('pour 1.0',15,box(300,0,800,500))
    add('via 0',15,ring,barrel='via 0')
    add('via 0',1,ring,barrel='via 0')
    add('trace 0',1,trace)
    add('pad U.P',1,box(145,45,155,55),terminal=('U','P'))
    board={'routes':{'copper_pours':[{'source_kind':'Poured','net':'REF','filled_polygons':[{}]},
                                    {'source_kind':'Poured','net':'REF','filled_polygons':[{}]}]}}
    drills=[dict(name='via 0',net='REF',layers=(1,15),inner=void,outer=void,plated=True)]
    return board,rows,drills


class IndependentAnchorTests(unittest.TestCase):
    def call(self,board,rows,drills,**options):
        with patch.object(repair,'rules_for',return_value=SimpleNamespace(contact_tolerance=0.01)),patch.object(repair,'copper_and_drills',return_value=(rows,drills)):
            return repair._reference_region(board,'REF',**options)

    def test_via_and_fixed_trace_anchor(self):
        board,rows,drills=fixture()
        result=self.call(board,rows,drills)
        self.assertEqual(result['name'],'pour 0.0')
        self.assertEqual(result['terminal'],('U','P'))
        self.assertLess(result['inner'].area,10001)

    def test_wrong_layer_projection_rejected(self):
        board,rows,drills=fixture()
        rows[-2]['layers']=(2,)
        with self.assertRaises(PipelineError):self.call(board,rows,drills)

    def test_npth_does_not_bridge_layers(self):
        board,rows,drills=fixture()
        drills[0]['plated']=False
        with self.assertRaises(PipelineError):self.call(board,rows,drills)

    def test_detached_annulus_does_not_use_barrel(self):
        board,rows,drills=fixture()
        detached=Point(50,50).buffer(6).difference(Point(50,50).buffer(4))
        for row in rows[2:4]:row['inner']=row['outer']=detached
        with self.assertRaises(PipelineError):self.call(board,rows,drills)

    def test_edge_touch_does_not_anchor(self):
        board,rows,drills=fixture()
        rows[-1]['inner']=rows[-1]['outer']=box(145,52,155,62)
        with self.assertRaises(PipelineError):self.call(board,rows,drills)

    def test_multi_island_geometry_not_joined(self):
        board,rows,drills=fixture()
        rows[0]['inner']=rows[0]['outer']=MultiPolygon([rows[0]['inner'],box(1000,0,1600,600)])
        result=self.call(board,rows,drills)
        self.assertLess(result['inner'].area,10001)

    def test_point_touch_does_not_anchor(self):
        board,rows,drills=fixture()
        rows[-1]['inner']=rows[-1]['outer']=box(150,52,160,62)
        with self.assertRaises(PipelineError):self.call(board,rows,drills)

    def test_explicit_budgets_do_not_claim_proof(self):
        board,rows,drills=fixture()
        for options in ({'max_nodes':1},{'max_contacts':1}):
            with self.assertRaisesRegex(PipelineError,'预算耗尽'):
                self.call(board,rows,drills,**options)


if __name__=='__main__':unittest.main(verbosity=2)
