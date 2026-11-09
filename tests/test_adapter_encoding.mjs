/** 来源编码兼容回归；合成来源、模拟 getter，不连接或改写真实 EDA。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {exportBoard, nativeSerializedCoordinateMatches, nativeViaDimensionMatches, validateViaAnchors} from '../eda_adapter.mjs';

const record=(type,id,payload)=>JSON.stringify({type,id})+'||'+JSON.stringify(payload)+'|';
const clone=structuredClone;
const types=['Component','Line','Pad','Via','Fill','Pour','Poured','Arc','Polyline','Region','String','Attribute','Dimension','Image','Object'];
const wrap=(type,id,v)=>Object.fromEntries(Object.entries({PrimitiveType:type,PrimitiveId:id,PrimitiveLock:false,...v}).map(([k,v])=>['getState_'+k,()=>v]));
function fixture({attached=false,layer=1,rotation=0}={}){
  const records=[['DOCHEAD',undefined,{docType:'PCB',uuid:'synthetic'}]],objects=Object.fromEntries(types.map(k=>[k,[]]));
  const corners=[[0,0],[300.1234,0],[300.1234,200.5678],[0,200.5678]];
  for(let i=0;i<4;i++){
    const a=corners[i],b=corners[(i+1)%4];
    records.push(['LINE','edge'+i,{layerId:11,startX:a[0],startY:a[1],endX:b[0],endY:b[1]}]);
    objects.Line.push({id:'edge'+i,Layer:11,StartX:a[0],StartY:a[1],EndX:b[0],EndY:b[1],LineWidth:1});
  }
  // 一个 getter 保留更多小数，而相邻 getter 已序列化；源码自己仍精确闭合。
  objects.Line[0].EndX=300.12341234;
  const context={pad_coordinates:'world',components:{}};
  if(attached){
    records.push(['COMPONENT','U',{layerId:layer,x:100,y:100,angle:rotation}]);
    const sourceVia={centerX:20,centerY:10,holeDiameter:11.812,viaDiameter:19.686,viaType:'NORMAL',netName:'GND'};
    const a=rotation*Math.PI/180,x=20*(layer===2?-1:1);
    objects.Component.push({id:'U',Designator:'U',X:100,Y:100,Rotation:rotation,Layer:layer,Footprint:{uuid:'fp'}});
    objects.Via.push({id:'Uv',X:100+x*Math.cos(a)-10*Math.sin(a),Y:100+x*Math.sin(a)+10*Math.cos(a),HoleDiameter:12,Diameter:20,ViaType:0,DesignRuleBlindViaName:null,Net:'GND'});
    context.pad_dimensions='footprint_source';
    context.footprint_via_movement='rigid';
    context.components.U={provenance:'合成封装',body_polygon:[[-5,-5],[5,-5],[5,5],[-5,5]],footprint_source:[record('DOCHEAD',undefined,{docType:'FOOTPRINT',uuid:'fp'}),record('VIA','v',sourceVia)].join('\n')};
  }
  const eda={dmt_SelectControl:{getCurrentDocumentInfo:async()=>({uuid:'synthetic',documentType:3})},
    sys_FileManager:{getDocumentSource:async()=>records.map(r=>record(...r)).join('\n')},
    pcb_Layer:{getAllLayers:async()=>[1,2,11].map(id=>({id,type:id===11?'OTHER':'SIGNAL',layerStatus:1}))}};
  for(const type of types)eda['pcb_Primitive'+type]={getAll:async()=>objects[type].map(v=>{
    const p=wrap(type,v.id,v);if(type==='Component')p.getAllPins=async()=>[];return p;
  })};
  return {eda,context,objects,records};
}

test('板框用同源四位小数编码闭环，原始 getter 保持不变并输出差异证据',async()=>{
  const m=fixture(),before=clone(m.objects);const b=await exportBoard(m.eda,m.context);
  assert.equal(b.board_bbox.outline.length,4);assert.equal(b.metadata.outline_endpoint_encoding_proofs.length,1);
  assert.equal(b.metadata.outline_endpoint_encoding_proofs[0].api,300.12341234);
  assert.deepEqual(m.objects,before);
});
test('开口、错误层、缺源码和非编码坐标不被自动闭合',async()=>{
  for(const mutate of [m=>m.records[2][2].startX+=.0001,m=>m.objects.Line[0].EndX+=.01,
    m=>m.records[1][2].layerId=1,m=>delete m.records[1][2].endX]){
    const m=fixture();mutate(m);await assert.rejects(exportBoard(m.eda,m.context));
  }
});
test('旧精确 API 板框仍可用，浮点几何容差不扩大',async()=>{
  const m=fixture();m.objects.Line[0].EndX=300.1234;
  m.records.forEach(r=>{if(r[0]==='LINE')r[2]={};});
  assert.equal((await exportBoard(m.eda,m.context)).board_bbox.outline.length,4);
  for(const [a,b] of [[1,1.00001],[1.0001,1],[NaN,0],[.12344,.1235]])assert.equal(nativeSerializedCoordinateMatches(a,b),false);
  assert(nativeSerializedCoordinateMatches(-1.23456,-1.2346));
});
test('附属孔只在显式源码模式保留真实尺寸和顶底层旋转姿态',async()=>{
  for(const layer of [1,2])for(const rotation of [0,90,37]){
    const m=fixture({attached:true,layer,rotation}),b=await exportBoard(m.eda,m.context),v=b.routes.vias[0];
    assert.equal(v.hole,11.812);assert.equal(v.diameter,19.686);
    assert.equal(v.metadata.api_hole_diameter,12);assert.equal(v.metadata.api_copper_diameter,20);
    assert.equal(v.metadata.parent_component_id,'U');assert.equal(b.components[0].fixed,false);
  }
});
test('附属孔拒绝默认模式、错误尺寸/坐标/身份/类型/实例覆盖',async()=>{
  for(const mutate of [m=>delete m.context.pad_dimensions,m=>m.objects.Via[0].HoleDiameter=13,
    m=>m.objects.Via[0].X+=.001,m=>m.objects.Component[0].Footprint.uuid='wrong',
    m=>m.objects.Via[0].ViaType=1,m=>m.objects.Via[0].Net='WRONG',
    m=>m.records.push(['VIA','Uv',{centerX:120,centerY:110,holeDiameter:12,viaDiameter:20}])]){
    const m=fixture({attached:true});mutate(m);await assert.rejects(exportBoard(m.eda,m.context));
  }
  for(const values of [[12,11.812,'api'],[20.1,19.686,'footprint_source'],[0,.1,'footprint_source'],[NaN,12,'footprint_source']])assert(!nativeViaDimensionMatches(...values));
});

function calibration(){
  const parent='p',poured=JSON.stringify(['POURED',parent]);
  const points=[[11,17],[23,31],[37,43],[53,59],[67,79],[83,97],[101,113],[127,139]];
  const paths=points.map(([x,y])=>[x+1.2,y,'ARC',180,x-1.2,y,'ARC',180,x+1.2,y]);
  const rows=[['DOCHEAD',undefined,{docType:'PCB',uuid:'synthetic'}],['POUR',parent,{netName:'GND'}],
    ['POURED',poured,{pourFill:[{id:'r',fill:true,strokeWidth:0,path:paths}]}]];
  const via_api=points.map(([x,y],i)=>{
    rows.push(['VIA','v'+i,{centerX:x*10,centerY:y*10,viaType:'NORMAL',netName:'OTHER',holeDiameter:6,viaDiameter:12}]);
    return {id:'v'+i,x:x*10+.00003,y:y*10,net:'OTHER',hole:6,diameter:12,via_type:0};
  });
  return {method:'native_poured_via_anchors',source_native:rows.map(r=>record(...r)).join('\n'),pour_id:parent,poured_id:poured,region_id:'r',raw_paths:paths,via_api,tolerance_mil:1e-7};
}
test('锚点用精确源码拟合，保留原测量和原 1e-7 mil 残差',()=>{
  const c=calibration(),before=clone(c),p=validateViaAnchors(c,'synthetic',10);
  assert.equal(p.matched_circle_count,8);assert.equal(p.getter_position_quantization_proofs.length,8);assert.equal(p.tolerance_mil,1e-7);
  assert.deepEqual(c,before);
});
test('锚点拒绝错误单位、孔径、网络、来源、编码与伪造圆心',()=>{
  for(const mutate of [c=>c.via_api[0].x*=10,c=>c.via_api[0].x+=.01,c=>c.via_api[0].hole=7,
    c=>c.via_api[0].net='wrong',c=>c.source_native=c.source_native.replace('"synthetic"','"other"'),
    c=>c.raw_paths[0][0]+=.000001,c=>c.tolerance_mil=.01]){
    const c=calibration();mutate(c);assert.throws(()=>validateViaAnchors(c,'synthetic',10));
  }
});
