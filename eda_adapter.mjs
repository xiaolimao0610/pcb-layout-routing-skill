/** EasyEDA adapter. Documented API 1.1.36; no live-board validation claimed. */
export const ENUMS = Object.freeze({
  DocumentType: { PCB: 3 },
  Layer: { TOP: 1, BOTTOM: 2, BOARD_OUTLINE: 11, MULTI: 12 },
  LayerStatus: { NOT_USED: 0, SHOW: 1, HIDDEN: 2 },
  PadShape: { RECTANGLE: 'RECT', ELLIPSE: 'ELLIPSE', OBLONG: 'OVAL', POLYGON: 'POLYGON' },
  Hole: { ROUND: 'ROUND' }, Fill: { SOLID: 0 }, Via: { VIA: 0 },
  Unit: { MILLIMETER: 'mm' },
});
const MODULES = {
  Component: 'Component', Line: 'Line', Pad: 'Pad', Via: 'Via', Fill: 'Fill',
  Pour: 'Pour', Poured: 'Poured', Arc: 'Arc', Polyline: 'Polyline', Region: 'Region',
  String: 'String', Attribute: 'Attribute', Dimension: 'Dimension', Image: 'Image', Object: 'Object',
};
const SOURCE_TYPES = {
  COMPONENT: 'Component', LINE: 'Line', PAD: 'Pad', VIA: 'Via', FILL: 'Fill',
  POUR: 'Pour', POURED: 'Poured', ARC: 'Arc', CARC: 'Arc', POLY: 'Polyline',
  REGION: 'Region', STRING: 'String', TEXT: 'String', ATTR: 'Attribute',
  ATTRIBUTE: 'Attribute', DIMENSION: 'Dimension', IMAGE: 'Image', OBJ: 'Object', OBJECT: 'Object',
};
const SOURCE_ONLY_TYPES=new Set(['TEARDROP']);
const METADATA = new Set(['DOCHEAD', 'DOCTYPE', 'CANVAS', 'LAYER', 'LAYER_PHYS', 'ACTIVE_LAYER',
  'SILK_OPTS', 'PREFERENCE', 'ITEM_ORDER', 'NET', 'PRIMITIVE', 'GROUP', 'CONNECT', 'PROP',
  'EQLEN_GRP', 'PAD_NET', 'REUSE_BLOCK', 'RULE_TEMPLATE', 'RULE', 'RULE_SELECTOR',
  'META', 'ELE_PLACEHOLDER', 'PANELIZE', 'PANELIZE_STAMP', 'PANELIZE_SIDE']);
export class AdapterError extends Error {
  constructor(code, message, details = {}) { super(message); this.name = 'AdapterError'; this.code = code; this.details = details; }
}
const fail = (code, message, details) => { throw new AdapterError(code, message, details); };
const finite = (n, label, positive = false) => {
  if (typeof n !== 'number' || !Number.isFinite(n) || (positive && n <= 0)) fail('INVALID_NUMBER', label);
  return n;
};
const ident = (s, label) => { if (typeof s !== 'string' || !s.length) fail('INVALID_ID', label); return s; };
const array = (a, label) => { if (!Array.isArray(a)) fail('INVALID_ARRAY', label); return a; };
const copy = v => JSON.parse(JSON.stringify(v));
const state = (p, name) => {
  const fn = p?.['getState_' + name];
  if (typeof fn !== 'function') fail('MISSING_API', 'getState_' + name);
  return fn.call(p);
};
const id = p => ident(state(p, 'PrimitiveId'), 'primitiveId');
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const PAD_ALIAS_FIELDS=['X','Y','Rotation','Layer','Pad','Hole','HoleRotation','HoleOffsetX','HoleOffsetY','SpecialPad','Metallization','Net','PadNumber','PrimitiveLock'];
const COMPONENT_INSTANCE_FIELDS=new Set(['partitionId','groupId','layerId','x','y','positionX','positionY','angle','attrs','locked','zIndex']);
const near=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(a-b)<=Number.EPSILON*Math.max(1,Math.abs(a),Math.abs(b))*16;
const coordinateNear=(a,b,...operands)=>[a,b,...operands].every(Number.isFinite)&&Math.abs(a-b)<=Number.EPSILON*Math.max(1,...[a,b,...operands].map(Math.abs))*16;
/** 入参为 API 坐标及同 ID 原生源码坐标（mil）；返回编码是否互证，不修改坐标。
 * 仅接纳已有浮点误差或源码精确等于 API 的四位小数编码；无效数返回 false。
 * 这不是几何间距容差，禁止用它合并两个没有来源互证的端点。
 */
export function nativeSerializedCoordinateMatches(apiValue,sourceValue){
  return coordinateNear(apiValue,sourceValue)||(Number.isFinite(apiValue)&&Number.isFinite(sourceValue)&&
    Math.round(apiValue*10000)/10000===sourceValue&&Math.abs(apiValue-sourceValue)<=0.00005);
}
function traceCoveredByTraces(segment,traces){
  const dx=segment.ex-segment.sx,dy=segment.ey-segment.sy,length=Math.hypot(dx,dy);
  if(!Number.isFinite(length))return false;
  const tolerance=Number.EPSILON*Math.max(1,Math.abs(segment.sx),Math.abs(segment.sy),Math.abs(segment.ex),Math.abs(segment.ey))*16;
  const matching=traces.filter(t=>t.net===segment.net&&t.layer===segment.layer&&near(t.width,segment.width));
  if(length===0)return matching.some(t=>{
    const tx=t.ex-t.sx,ty=t.ey-t.sy,n=Math.hypot(tx,ty),px=segment.sx-t.sx,py=segment.sy-t.sy;
    if(!Number.isFinite(n))return false;
    if(n===0)return Math.hypot(px,py)<=tolerance;
    const along=px*(tx/n)+py*(ty/n);
    return Math.abs(px*(ty/n)-py*(tx/n))<=tolerance&&along>=-tolerance&&along<=n+tolerance;
  });
  const ux=dx/length,uy=dy/length,intervals=[];
  for(const t of matching){
    const ax=t.sx-segment.sx,ay=t.sy-segment.sy,bx=t.ex-segment.sx,by=t.ey-segment.sy;
    if(Math.abs(ax*uy-ay*ux)>tolerance||Math.abs(bx*uy-by*ux)>tolerance)continue;
    const a=ax*ux+ay*uy,b=bx*ux+by*uy;
    if(Number.isFinite(a)&&Number.isFinite(b))intervals.push([Math.min(a,b),Math.max(a,b)]);
  }
  let covered=0;
  for(const [start,end] of intervals.sort((a,b)=>a[0]-b[0])){
    if(end< -tolerance||start>length+tolerance)continue;
    if(start>covered+tolerance)return false;
    covered=Math.max(covered,end);
    if(covered>=length-tolerance)return true;
  }
  return false;
}
const sameTraceCoverage=(a,b)=>a.every(segment=>traceCoveredByTraces(segment,b))&&b.every(segment=>traceCoveredByTraces(segment,a));
const copperLayer = (n, stack) => stack.includes(n);
const checkLayer = (n, stack) => { if (!copperLayer(n, stack)) fail('INVALID_LAYER', 'Layer is not in copper stack: ' + n); return n; };

/** Portable SHA-256: works in the extension main process without Node/WebCrypto. */
export function fingerprint(text) {
  if (typeof text !== 'string') fail('INVALID_SOURCE', 'Document source must be text');
  const bytes = [];
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    if (cp < 128) bytes.push(cp);
    else if (cp < 2048) bytes.push(192 | cp >> 6, 128 | cp & 63);
    else if (cp < 65536) bytes.push(224 | cp >> 12, 128 | cp >> 6 & 63, 128 | cp & 63);
    else bytes.push(240 | cp >> 18, 128 | cp >> 12 & 63, 128 | cp >> 6 & 63, 128 | cp & 63);
  }
  return hashBytes(bytes);
}
function hashBytes(input) {
  const bytes=Array.from(input);
  const bitLen = bytes.length * 8; bytes.push(128);
  while (bytes.length % 64 !== 56) bytes.push(0);
  const hi = Math.floor(bitLen / 4294967296), lo = bitLen >>> 0;
  for (const n of [hi, lo]) for (let i = 3; i >= 0; i--) bytes.push(n >>> (8 * i) & 255);
  const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const k = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const r = (x, n) => x >>> n | x << (32 - n);
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const w = new Array(64);
    for (let i = 0; i < 16; i++) w[i] = bytes[offset+i*4] << 24 | bytes[offset+i*4+1] << 16 | bytes[offset+i*4+2] << 8 | bytes[offset+i*4+3];
    for (let i = 16; i < 64; i++) w[i] = (w[i-16] + (r(w[i-15],7)^r(w[i-15],18)^w[i-15]>>>3) + w[i-7] + (r(w[i-2],17)^r(w[i-2],19)^w[i-2]>>>10)) | 0;
    let [a,b,c,d,e,f,g,hh] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh+(r(e,6)^r(e,11)^r(e,25)) + ((e&f)^(~e&g)) + k[i]+w[i])|0;
      const t2 = ((r(a,2)^r(a,13)^r(a,22)) + ((a&b)^(a&c)^(b&c)))|0;
      hh=g;g=f;f=e;e=(d+t1)|0;d=c;c=b;b=a;a=(t1+t2)|0;
    }
    [a,b,c,d,e,f,g,hh].forEach((v,i)=>h[i]=(h[i]+v)|0);
  }
  return 'sha256:' + h.map(n=>(n>>>0).toString(16).padStart(8,'0')).join('');
}

function sourceRows(source) {
  if(typeof source!=='string')fail('INVALID_SOURCE','Document source must be text');
  const lines=source.split(/\r?\n/),last=lines.findLastIndex(line=>line.trim());
  const result = [];
  for (const [index, line] of lines.entries()) {
    const raw = line.trim(); if (!raw) continue;
    let header, payload;
    try {
      if (raw.startsWith('[')) { const row = JSON.parse(raw); header = { type: row[0], id: row[1] }; payload = { legacy: row }; }
      else {
        const split = raw.indexOf('||');
        if (split < 0 || (!raw.endsWith('|')&&index!==last)) fail('UNSUPPORTED_SOURCE_FORMAT', 'Expected one complete record per line; only the final terminator may be absent');
        header = JSON.parse(raw.slice(0, split)); payload = JSON.parse(raw.slice(split+2,raw.endsWith('|')?-1:undefined));
      }
    } catch (error) {
      if (error instanceof AdapterError) throw error;
      fail('UNSUPPORTED_SOURCE_FORMAT', 'Cannot parse source record ' + (index+1));
    }
    if(!header||typeof header!=='object'||typeof header.type!=='string')fail('UNSUPPORTED_SOURCE_FORMAT','Missing record type at line '+(index+1));
    result.push({header,payload,index,line,legacy:raw.startsWith('[')});
  }
  return result;
}
/** Source identity v1: only the three observed DOCHEAD transport fields are volatile.
 * Unknown records and all geometry, rules, nets, tickets and other metadata stay hashed. */
export function sourceFingerprint(source) {
  const rows=sourceRows(source),lines=source.split(/(\r?\n)/);
  for(const r of rows)if(r.header.type==='DOCHEAD'&&!r.legacy){
    const split=r.line.indexOf('||')+2,end=r.line.trim().endsWith('|')?r.line.lastIndexOf('|'):r.line.length;
    lines[r.index*2]=r.line.slice(0,split)+withoutVolatileHeaderFields(r.line.slice(split,end))+r.line.slice(end);
  }
  return fingerprint(lines.join(''));
}
function withoutVolatileHeaderFields(text) {
  return withoutObjectFields(text,['client','updateTime','version'],'DOCHEAD payload');
}
function withoutObjectFields(text,fields,label) {
  const start=text.indexOf('{'),end=text.lastIndexOf('}');
  if(start<0||end<start)fail('UNSUPPORTED_SOURCE_FORMAT',label+' must be an object');
  const members=[];let from=start+1,depth=0,quoted=false,escaped=false;
  for(let i=from;i<end;i++){
    const ch=text[i];
    if(quoted){if(escaped)escaped=false;else if(ch==='\\')escaped=true;else if(ch==='"')quoted=false;}
    else if(ch==='"')quoted=true;
    else if(ch==='{'||ch==='[')depth++;
    else if(ch==='}'||ch===']')depth--;
    else if(ch===','&&depth===0){members.push(text.slice(from,i));from=i+1;}
  }
  members.push(text.slice(from,end));
  const kept=members.filter(member=>{
    const key=member.match(/^\s*("(?:[^"\\]|\\.)*")\s*:/);
    return !key||!fields.includes(JSON.parse(key[1]));
  });
  return text.slice(0,start+1)+kept.join(',')+text.slice(end);
}
/** Rollback-only identity: unique current PCB records, preserving every raw content field. */
function rollbackContentFingerprint(source){
  const rows=sourceRows(source),heads=rows.filter(r=>r.header.type==='DOCHEAD');
  if(rows.some(r=>r.legacy||r.header.type==='DOCTYPE')||rows[0]?.header.type!=='DOCHEAD'||heads.length!==1||heads[0].payload.docType!=='PCB'||typeof heads[0].payload.uuid!=='string'||!heads[0].payload.uuid)fail('UNPROVEN_RECORD_CONTENT','Record-content rollback verification requires one current PCB document');
  const seen=new Set(),contents=[];
  for(const r of rows){
    const key=r.header.id;
    if(typeof key==='number'&&!Number.isSafeInteger(key))fail('UNSAFE_RECORD_ID','Numeric record IDs must be safe integers');
    if(key!==undefined&&key!==null&&!['string','number'].includes(typeof key))fail('UNPROVEN_RECORD_CONTENT','Unsupported record identity type');
    const identity=JSON.stringify([r.header.type,typeof key,key??null]);
    if(seen.has(identity))fail('DUPLICATE_RECORD_IDENTITY','History or duplicate record identity prevents rollback fallback');
    seen.add(identity);
    const split=r.line.indexOf('||'),end=r.line.trim().endsWith('|')?r.line.lastIndexOf('|'):r.line.length;
    const header=withoutObjectFields(r.line.slice(0,split),['ticket'],'Record header');
    const rawPayload=r.line.slice(split+2,end),payload=r.header.type==='DOCHEAD'?withoutVolatileHeaderFields(rawPayload):rawPayload;
    contents.push([identity,header,payload]);
  }
  contents.sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0);
  return fingerprint(JSON.stringify(contents));
}
/** Does not weaken sourceFingerprint or authorize replay of an old plan. */
export function verifyRollbackSource(original,restored){
  let currentFingerprint;
  try{
    currentFingerprint=sourceFingerprint(restored);
    if(sourceFingerprint(original)===currentFingerprint)return {restored:true,verification:'source_fingerprint',source_fingerprint:currentFingerprint,requires_reexport:false};
    const before=rollbackContentFingerprint(original),after=rollbackContentFingerprint(restored);
    if(before===after)return {restored:true,verification:'record_content',source_fingerprint:currentFingerprint,record_content_fingerprint:after,requires_reexport:true};
    return {restored:false,verification:null,source_fingerprint:currentFingerprint,requires_reexport:true};
  }catch(error){return {restored:false,verification:null,source_fingerprint:currentFingerprint,requires_reexport:true,error:{code:error.code??'API_ERROR',message:error.message}};}
}
/** Inventory both documented record encodings. Unknown record types fail closed. */
export function inventorySource(source) {
  const result=sourceRows(source).map(({header,payload,index,line,legacy})=>{
    if (!METADATA.has(header.type) && !SOURCE_TYPES[header.type] && !SOURCE_ONLY_TYPES.has(header.type)) {
      const split=line.indexOf('||'),end=line.trim().endsWith('|')?line.lastIndexOf('|'):line.length;
      const rawHeader=legacy?'':line.slice(0,split),rawPayload=legacy?line:line.slice(split+2,end);
      fail('UNKNOWN_SOURCE_PRIMITIVE', header.type, {line:index+1,
        raw_header:rawHeader.slice(0,1024),raw_header_length:rawHeader.length,raw_header_truncated:rawHeader.length>1024,
        raw_payload:rawPayload.slice(0,8192),raw_payload_length:rawPayload.length,raw_payload_truncated:rawPayload.length>8192});
    }
    if(header.type==='TEARDROP'){
      if(legacy||Object.keys(header).some(k=>!['type','id','ticket'].includes(k))||typeof header.id!=='string'||!header.id||!Number.isSafeInteger(header.ticket)||header.ticket<0||
        !payload||Array.isArray(payload)||!eq(Object.keys(payload).sort(),['partitionId','netName','layerId','path','groupId','refs','locked'].sort())||
        !['',null].includes(payload.partitionId)||payload.groupId!==0||payload.locked!==false)fail('UNPROVEN_TEARDROP_SCHEMA','Unsupported native TEARDROP record',{line:index+1});
    }
    if(header.type==='ELE_PLACEHOLDER'&&(!payload||Object.keys(payload).some(k=>!['dataType','max'].includes(k))||typeof payload.dataType!=='string'||!Number.isInteger(payload.max)||payload.max<0))fail('UNSUPPORTED_METADATA','Unproved ELE_PLACEHOLDER schema',{line:index+1});
    return {type:header.type,id:header.id,payload,...(header.type==='TEARDROP'?{source_header:header}:{})};
  });
  if (!result.some(r=>r.type==='DOCHEAD'||r.type==='DOCTYPE')) fail('INVALID_SOURCE', 'Missing document header');
  const panel=result.find(r=>r.type==='PANELIZE');
  if(panel&&panel.payload.on!==false&&panel.payload.on!==0)fail('UNSUPPORTED_PANELIZATION','Enabled or unproved panelization needs explicit panel geometry');
  if(!panel&&result.some(r=>['PANELIZE_STAMP','PANELIZE_SIDE'].includes(r.type)))fail('UNSUPPORTED_PANELIZATION','Panel settings have no disabled PANELIZE parent');
  return result;
}

function ring(points) {
  array(points, 'polygon points');
  const p = points.map(v => { array(v,'point'); if(v.length!==2) fail('INVALID_POLYGON','2D point required'); return [finite(v[0],'point.x'),finite(v[1],'point.y')]; });
  if (p.length>1 && eq(p[0],p.at(-1))) p.pop();
  if (p.length<3) fail('INVALID_POLYGON','At least 3 polygon vertices required');
  let area=0; for(let i=0;i<p.length;i++) area+=p[i][0]*p[(i+1)%p.length][1]-p[(i+1)%p.length][0]*p[i][1];
  if (!area) fail('INVALID_POLYGON','Zero-area polygon');
  return p;
}
/** No discretization: arcs, circles and Beziers in paths are rejected. */
function polygonSource(src) {
  array(src,'polygon source');
  if (!src.length) fail('INVALID_POLYGON','Empty polygon');
  if (Array.isArray(src[0])) return {points: parseStraightRing(src[0]), holes:src.slice(1).map(parseStraightRing)};
  return {points:parseStraightRing(src),holes:[]};
}
function parseStraightRing(src) {
  array(src,'single polygon');
  const vals=[];
  for (const v of src) { if (v==='L') continue; if(typeof v!=='number') fail('UNSUPPORTED_CURVE','Only straight L polygon paths are supported'); vals.push(finite(v,'polygon coordinate')); }
  if(vals.length%2) fail('INVALID_POLYGON','Unpaired polygon coordinate');
  const points=[]; for(let i=0;i<vals.length;i+=2) points.push([vals[i],vals[i+1]]);
  return ring(points);
}
/** Native Poured paths are preserved; topology is checked by the local solver. */
export function nativeCompoundPaths(paths,scale=1){
  finite(scale,'native copper coordinate scale',true);
  if(!Array.isArray(paths)||!paths.length||paths.length>2048)fail('INVALID_NATIVE_POUR','Complex native path rings required');
  return paths.map(path=>{
    if(!Array.isArray(path)||path.length<7||path.length>65536)fail('INVALID_NATIVE_POUR','Bounded native L/ARC path required');
    const out=[finite(path[0],'native x')*scale,finite(path[1],'native y')*scale];let i=2,line=false;
    while(i<path.length){
      const token=path[i];
      if(typeof token==='string'){
        i++;
        if(token==='L'){out.push(token);line=true;}
        else if(token==='ARC'&&i+2<path.length){
          const angle=finite(path[i++],'native angle');
          if(!angle||Math.abs(angle)>=360)fail('INVALID_NATIVE_POUR','Nonzero two-endpoint ARC strictly below 360 degrees required');
          out.push(token,angle,finite(path[i++],'native x')*scale,finite(path[i++],'native y')*scale);line=false;
        }else fail('INVALID_NATIVE_POUR','Only documented L/ARC commands are supported');
      }else{
        if(!line||i+1>=path.length)fail('INVALID_NATIVE_POUR','Native XY requires L/ARC command');
        out.push(finite(path[i++],'native x')*scale,finite(path[i++],'native y')*scale);
      }
    }
    if(out.some(v=>typeof v==='number'&&!Number.isFinite(v)))fail('INVALID_NATIVE_POUR','Native coordinate conversion overflow');
    return out;
  });
}
/** 将已验证长度单位的单段原生铜笔画转为 mil；非法或未支持路径立即报错。 */
export function nativeStraightStroke(paths,width,scale=1){
  finite(scale,'native stroke scale',true);finite(width,'native stroke width',true);
  if(!Array.isArray(paths)||paths.length!==1||!Array.isArray(paths[0])||paths[0].length!==5||paths[0][2]!=='L')
    fail('UNSUPPORTED_NATIVE_STROKE','Only one explicit straight L copper stroke is supported');
  const p=paths[0],points=[[finite(p[0],'stroke x')*scale,finite(p[1],'stroke y')*scale],[finite(p[3],'stroke x')*scale,finite(p[4],'stroke y')*scale]];
  if(points[0][0]===points[1][0]&&points[0][1]===points[1][1])fail('UNSUPPORTED_NATIVE_STROKE','Degenerate copper stroke');
  if(!points.flat().every(Number.isFinite)||!Number.isFinite(width*scale))fail('INVALID_NATIVE_POUR','Stroke conversion overflow');
  return {native_stroke_path:{points,width:width*scale}};
}
function compareNativePaths(a,b,scale,tolerance){
  a=nativeCompoundPaths(a,scale);b=nativeCompoundPaths(b);
  if(a.length!==b.length)fail('POUR_CALIBRATION_MISMATCH','Native ring counts differ');
  let maximum=0,coordinates=0,angles=0;
  for(let j=0;j<a.length;j++){
    if(a[j].length!==b[j].length)fail('POUR_CALIBRATION_MISMATCH','Native path structures differ');
    for(let i=0;i<a[j].length;i++){
      const x=a[j][i],y=b[j][i];
      if(typeof x==='string'||typeof y==='string'){
        if(x!==y)fail('POUR_CALIBRATION_MISMATCH','Native path commands differ');
      }else if(i&&a[j][i-1]==='ARC'){
        if(x!==y)fail('POUR_CALIBRATION_MISMATCH','ARC angle changed during native conversion');angles++;
      }else{
        const error=Math.abs(x-y);
        if(error>tolerance)fail('POUR_CALIBRATION_MISMATCH','Native XY mapping differs',{ring:j,index:i,error,tolerance});
        maximum=Math.max(maximum,error);coordinates++;
      }
    }
  }
  return {coordinate_pairs:coordinates/2,arc_angles_verified:angles,max_error_mil:maximum};
}
/** The second argument is a real native FILL record, whose path uses PCB mil. */
export function validateConversion(raw,nativeFillSource,toleranceMil=1e-7){
  finite(toleranceMil,'native conversion tolerance',true);
  if(toleranceMil>0.001)fail('POUR_CALIBRATION_MISMATCH','Native conversion tolerance must not exceed 0.001 mil');
  const rows=typeof nativeFillSource==='string'?sourceRows(nativeFillSource):[nativeFillSource];
  if(rows.length!==1||rows[0]?.header?.type!=='FILL'||rows[0]?.legacy||rows[0]?.payload?.fillStyle!=='SOLID')fail('POUR_CALIBRATION_MISMATCH','One real solid FILL source record required');
  const paths=nativeCompoundPaths(raw),target=nativeCompoundPaths(rows[0].payload.path);
  if(paths.length!==target.length)fail('POUR_CALIBRATION_MISMATCH','Conversion changed ring count');
  let largest=0,scale;
  for(let j=0;j<paths.length;j++){
    if(paths[j].length!==target[j].length)fail('POUR_CALIBRATION_MISMATCH','Conversion changed path structure');
    for(let i=0;i<paths[j].length;i++)if(typeof paths[j][i]==='number'&&!(i&&paths[j][i-1]==='ARC')&&Math.abs(paths[j][i])>largest){
      largest=Math.abs(paths[j][i]);scale=target[j][i]/paths[j][i];
    }
  }
  if(!largest)fail('POUR_CALIBRATION_MISMATCH','No nonzero coordinates establish a unique scale');
  finite(scale,'proven native coordinate scale',true);
  return {coordinate_scale:scale,tolerance_mil:toleranceMil,...compareNativePaths(paths,target,scale,toleranceMil)};
}
export function validateViaAnchors(c,documentUuid,expectedScale){
  strictKeys(c,['method','source_native','pour_id','poured_id','region_id','raw_paths','via_api','tolerance_mil'],'via anchor calibration');
  if(c.method!=='native_poured_via_anchors')fail('UNPROVEN_POUR_COORDINATES','Actual via anchor calibration required');
  const tolerance=c.tolerance_mil??1e-7;finite(tolerance,'via anchor tolerance',true);
  if(tolerance>1e-6)fail('UNPROVEN_POUR_COORDINATES','Via anchor matching must be precise, not a broad spatial match');
  const rows=sourceRows(c.source_native),heads=rows.filter(r=>r.header.type==='DOCHEAD');
  if(heads.length!==1||heads[0].payload.docType!=='PCB'||heads[0].payload.uuid!==documentUuid)fail('POUR_CALIBRATION_MISMATCH','Via calibration source belongs to another PCB');
  const parents=rows.filter(r=>r.header.type==='POUR'&&r.header.id===c.pour_id),poured=rows.filter(r=>r.header.type==='POURED'&&r.header.id===c.poured_id);
  if(parents.length!==1||poured.length!==1)fail('POUR_CALIBRATION_MISMATCH','Unique native Pour and Poured records required');
  let identity;try{identity=JSON.parse(c.poured_id);}catch(_){identity=null;}
  if(!eq(identity,['POURED',c.pour_id]))fail('POUR_CALIBRATION_MISMATCH','Native Poured parent identity is unproven');
  const regions=poured[0].payload.pourFill?.filter(r=>r.id===c.region_id);
  if(regions?.length!==1||regions[0].fill!==true||regions[0].strokeWidth!==0)fail('POUR_CALIBRATION_MISMATCH','Actual filled native anchor region required');
  compareNativePaths(c.raw_paths,regions[0].path,1,0);
  const api=array(c.via_api,'actual via getters'),apiIds=new Set(api.map(v=>v.id));
  if(apiIds.size!==api.length)fail('POUR_CALIBRATION_MISMATCH','Duplicate via API identities');
  const vias=[],getter_position_quantization_proofs=[];
  for(const r of rows.filter(r=>r.header.type==='VIA')){
    const v=r.payload,a=api.find(a=>a.id===r.header.id);
    if(!a||v.viaType!=='NORMAL'||a.via_type!==0||a.net!==v.netName||!['x','y','hole','diameter'].every(k=>Number.isFinite(a[k])))fail('POUR_CALIBRATION_MISMATCH','Actual VIA source/getter identity differs');
    for(const [k,field] of [['x','centerX'],['y','centerY']]){
      if(!nativeSerializedCoordinateMatches(a[k],v[field]))fail('POUR_CALIBRATION_MISMATCH','VIA source and getter position encoding differs');
      if(!coordinateNear(a[k],v[field]))getter_position_quantization_proofs.push({id:a.id,field:k,api:a[k],native_source:v[field],error_mil:Math.abs(a[k]-v[field]),serialization_decimals:4});
    }
    for(const [k,field] of [['hole','holeDiameter'],['diameter','viaDiameter']])if(!coordinateNear(a[k],v[field]))fail('POUR_CALIBRATION_MISMATCH','VIA source and getter units differ');
    if(a.net!==parents[0].payload.netName)vias.push({...a,x:v.centerX,y:v.centerY});
  }
  const circles=[];
  nativeCompoundPaths(c.raw_paths).forEach((p,i)=>{
    if(p.length===10&&p[2]==='ARC'&&p[6]==='ARC'&&p[3]===p[7]&&Math.abs(p[3])===180&&coordinateNear(p[0],p[8])&&coordinateNear(p[1],p[9]))circles.push({ring_index:i,x:(p[0]+p[4])/2,y:(p[1]+p[5])/2,radius:Math.hypot(p[0]-p[4],p[1]-p[5])/2});
  });
  const seeds=[],extent=Math.max(1,...circles.flatMap(q=>[Math.abs(q.x),Math.abs(q.y)]));
  for(const q of circles)for(const v of vias){
    const scale=Math.abs(q.x)>Math.abs(q.y)?v.x/q.x:v.y/q.y;
    if(Number.isFinite(scale)&&scale>0&&Math.abs(q.x*scale-v.x)<=tolerance&&Math.abs(q.y*scale-v.y)<=tolerance&&q.radius*scale>v.diameter/2&&!seeds.some(s=>Math.abs(s-scale)*extent<=tolerance))seeds.push(scale);
  }
  const supported=[];
  for(const scale of seeds){
    const matches=[];let ambiguous=false;
    for(const q of circles){
      const found=vias.filter(v=>Math.abs(q.x*scale-v.x)<=tolerance&&Math.abs(q.y*scale-v.y)<=tolerance&&q.radius*scale>v.diameter/2);
      if(found.length>1){ambiguous=true;break;}
      if(found.length===1)matches.push({...q,via_id:found[0].id,mil_x:found[0].x,mil_y:found[0].y});
    }
    if(ambiguous||matches.length<8||new Set(matches.map(m=>m.via_id)).size!==matches.length)continue;
    const a=matches[0],b=matches.find(b=>Math.hypot(a.x-b.x,a.y-b.y)>tolerance);
    if(!b||!matches.some(q=>Math.abs((b.x-a.x)*(q.y-a.y)-(b.y-a.y)*(q.x-a.x))>tolerance*tolerance))continue;
    supported.push({scale,matches});
  }
  if(supported.length!==1)fail('UNPROVEN_POUR_COORDINATES','Exactly one scale must have at least eight unambiguous, non-collinear actual VIA anchors',{supported_scales:supported.map(s=>s.scale)});
  const {matches}=supported[0],n=matches.length;
  const mean=k=>matches.reduce((s,m)=>s+m[k],0)/n;
  const mx=mean('x'),my=mean('y'),vx=mean('mil_x'),vy=mean('mil_y');
  const numerator=matches.reduce((s,m)=>s+(m.x-mx)*(m.mil_x-vx)+(m.y-my)*(m.mil_y-vy),0),denominator=matches.reduce((s,m)=>s+(m.x-mx)**2+(m.y-my)**2,0);
  const scale=numerator/denominator,translation=[vx-scale*mx,vy-scale*my];
  if(!Number.isFinite(scale)||scale<=0||translation.some(v=>Math.abs(v)>tolerance)||matches.some(m=>Math.abs(m.x*scale-m.mil_x)>tolerance||Math.abs(m.y*scale-m.mil_y)>tolerance))fail('POUR_CALIBRATION_MISMATCH','Via anchor affine fit does not prove a common PCB origin');
  if(expectedScale!==undefined&&(!Number.isFinite(expectedScale)||matches.some(m=>Math.abs(m.x*expectedScale-m.mil_x)>tolerance||Math.abs(m.y*expectedScale-m.mil_y)>tolerance)))fail('POUR_CALIBRATION_MISMATCH','Supplied scale differs from measured actual VIA anchors');
  return {coordinate_scale:scale,translation_mil:translation,tolerance_mil:tolerance,matched_circle_count:n,circle_count:circles.length,native_foreign_via_count:vias.length,matches,unmatched_circle_indices:circles.filter(q=>!matches.some(m=>m.ring_index===q.ring_index)).map(q=>q.ring_index),getter_position_quantization_proofs};
}
function nativePourProfile(profile,documentUuid,source){
  if(!profile)return null;
  strictKeys(profile,['source','coordinate_scale','calibration'],'native pour geometry profile');
  if(!eq(profile.source,{document_uuid:documentUuid,fingerprint:sourceFingerprint(source)}))fail('STALE_POUR_PROFILE','Native pour coordinate profile must bind to this actual source');
  const c=profile.calibration;
  if(c?.method==='native_poured_via_anchors')return {...copy(profile),verification:validateViaAnchors(c,documentUuid,profile.coordinate_scale),mode:'local_preview_only'};
  if(!c||c.method!=='native_poured_convert_to_fill')fail('UNPROVEN_POUR_COORDINATES','Measured convertToFill calibration is required');
  strictKeys(c,['method','source_before','source_converted','poured_id','region_id','fill_id','raw_paths','tolerance_mil'],'native pour calibration');
  const before=sourceRows(c.source_before),converted=sourceRows(c.source_converted);
  for(const rows of [before,converted]){
    const heads=rows.filter(r=>r.header.type==='DOCHEAD');
    if(heads.length!==1||heads[0].payload.docType!=='PCB'||heads[0].payload.uuid!==documentUuid)fail('POUR_CALIBRATION_MISMATCH','Calibration must belong to this physical PCB');
  }
  const poured=before.filter(r=>r.header.type==='POURED'&&r.header.id===c.poured_id),fills=converted.filter(r=>r.header.type==='FILL'&&r.header.id===c.fill_id);
  if(poured.length!==1||fills.length!==1||before.some(r=>r.header.type==='FILL'&&r.header.id===c.fill_id))fail('POUR_CALIBRATION_MISMATCH','Unique original Poured and newly converted Fill records required');
  const regions=poured[0].payload.pourFill?.filter(r=>r.id===c.region_id);
  if(regions?.length!==1||regions[0].fill!==true||regions[0].strokeWidth!==0)fail('POUR_CALIBRATION_MISMATCH','Original filled region is missing or unsupported');
  compareNativePaths(c.raw_paths,regions[0].path,1,0);
  const proof=validateConversion(c.raw_paths,fills[0],c.tolerance_mil??1e-7);
  // Verify the supplied scale itself over every coordinate; never trust a tag.
  compareNativePaths(c.raw_paths,fills[0].payload.path,finite(profile.coordinate_scale,'coordinate scale',true),proof.tolerance_mil);
  return {...copy(profile),verification:{...proof,source_before:sourceFingerprint(c.source_before),source_converted:sourceFingerprint(c.source_converted)},mode:'local_preview_only'};
}
/** Native simple polygon grammar proven for source-only TEARDROP; points are chord previews only. */
function nativePathRegion(path){
  array(path,'native_path');let i=0;
  const point=()=>[finite(path[i++],'native path x'),finite(path[i++],'native path y')];
  const first=point(),segments=[];let current=first;
  while(i<path.length){
    const command=path[i++];
    if(command==='L'){
      const start=i;
      while(i<path.length&&typeof path[i]!=='string'){
        const end=point();if(eq(end,current))fail('UNSUPPORTED_NATIVE_PATH','Zero-length native line');
        segments.push({start:current,end,angle:0});current=end;
      }
      if(i===start)fail('UNSUPPORTED_NATIVE_PATH','Native L needs an endpoint');
    }else if(command==='ARC'){
      const angle=finite(path[i++],'native arc angle'),end=point();
      if(!angle||Math.abs(angle)>=180||eq(current,end))fail('UNSUPPORTED_NATIVE_PATH','Only nonzero minor ARC sweeps are supported');
      segments.push({start:current,end,angle});current=end;
    }else fail('UNSUPPORTED_NATIVE_PATH','Only explicit native L/ARC commands are supported');
  }
  if(!eq(current,first))fail('UNSUPPORTED_NATIVE_PATH','Native path must be explicitly closed');
  return {points:ring(segments.map(s=>s.start)),native_path:copy(path),segments};
}
function readTeardrops(records,objects,stack){
  const refs=new Map();
  for(const type of ['Line','Pad','ComponentPad','Via'])for(const p of objects[type])refs.set(id(p),{p,type:type==='ComponentPad'?'Pad':type});
  const seen=new Set(),apiIds=new Set(Object.values(objects).flat().map(id));
  return records.filter(r=>r.type==='TEARDROP').map(r=>{
    const v=r.payload;
    if(seen.has(r.id)||apiIds.has(r.id))fail('DUPLICATE_PRIMITIVE','Repeated native TEARDROP identity '+r.id);seen.add(r.id);
    ident(v.netName,'teardrop net');checkLayer(v.layerId,stack);
    const ids=array(v.refs,'teardrop refs');
    if(ids.some(x=>typeof x!=='string'||!x)||new Set(ids).size!==ids.length)fail('UNPROVEN_TEARDROP_REFS','TEARDROP refs must be unique primitive IDs');
    const entries=ids.map(x=>refs.get(x)),types=entries.map(x=>x?.type).sort().join(',');
    if(!['Line,Pad','Line,Via','Line,Line','Line,Line,Line','Line,Line,Line,Line'].includes(types))fail('UNPROVEN_TEARDROP_REFS','Unproved TEARDROP reference combination');
    for(const {p,type} of entries){
      if(state(p,'Net')!==v.netName||(type!=='Via'&&state(p,'Layer')!==v.layerId&&!(type==='Pad'&&state(p,'Layer')===ENUMS.Layer.MULTI)))fail('TEARDROP_REFERENCE_MISMATCH','TEARDROP net/layer differs from referenced primitive '+id(p));
      if(type==='Line'&&records.filter(x=>x.type==='LINE'&&x.id===id(p)).length!==1)fail('UNPROVEN_TEARDROP_REFS','TEARDROP line needs unique native source identity');
    }
    const {points,native_path}=nativePathRegion(v.path);
    return {id:r.id,net:v.netName,layer:v.layerId,locked:true,source_kind:'Teardrop',filled_polygons:[{points,native_path}],
      metadata:{source_only:true,refs:copy(ids),native_source_record:{header:copy(r.source_header),payload:copy(v)}}};
  });
}
const radians = a => a*Math.PI/180;
function inversePoint(x,y,c) {
  const a=radians(c.rotation), dx=x-c.x, dy=y-c.y;
  return [(dx*Math.cos(a)+dy*Math.sin(a))*(c.layer===ENUMS.Layer.BOTTOM?-1:1), -dx*Math.sin(a)+dy*Math.cos(a)];
}
function nativePadDimensions(raw,sourcePad,pid){
  const native=sourcePad?.payload,shape=native?.defaultPad;
  if(!native||native.legacy||!Array.isArray(native.specialPad)||native.specialPad.length||!shape||typeof shape!=='object'||Array.isArray(shape))fail('UNPROVEN_PAD_DIMENSIONS','Complete native defaultPad with no per-layer override required: '+pid);
  const rect=shape.padType==='RECT',supported=['RECT','ELLIPSE','OVAL'].includes(shape.padType),keys=rect?['padType','width','height','radius']:['padType','width','height'];
  if(!supported||Object.keys(shape).some(k=>!keys.includes(k))||raw[0]!==shape.padType||raw.length!==(rect?4:3))fail('UNPROVEN_PAD_DIMENSIONS','Native/API pad shapes differ or are unsupported: '+pid);
  const values=[finite(shape.width,'native pad width',true),finite(shape.height,'native pad height',true),...(rect?[finite(shape.radius,'native pad radius')]:[])];
  if(!values.every((value,i)=>raw[i+1]===value||raw[i+1]===Number(value.toFixed(1))))fail('UNPROVEN_PAD_DIMENSIONS','Native dimensions do not match exact or measured 0.1 mil API dimensions: '+pid,{api_pad:copy(raw),native_default_pad:copy(shape)});
  return [shape.padType,...values];
}
function padGeometry(p, c, coordinateSpace, stack, sourcePad, rotationOverride, dimensionProfile='api',positionProfile='api') {
  const apiRaw=state(p,'Pad'), pid=id(p), layer=state(p,'Layer');
  if(!Array.isArray(apiRaw)) fail('UNPROVEN_PAD','No explicit pad geometry '+pid);
  const raw=dimensionProfile==='footprint_source'?nativePadDimensions(apiRaw,sourcePad,pid):apiRaw;
  if(!Array.isArray(raw)) fail('UNPROVEN_PAD','No explicit pad geometry '+pid);
  if(state(p,'SpecialPad')?.length) fail('UNSUPPORTED_PAD','Per-layer special pad '+pid);
  const hole=state(p,'Hole');
  const slot=hole?.[0]==='SLOT'&&hole.length===3;
  if(hole && !slot&&(hole[0]!==ENUMS.Hole.ROUND || hole.length!==2)) fail('UNSUPPORTED_DRILL','Unsupported unproved drill tuple: '+pid,{primitive_id:pid,raw_hole:copy(hole),hole_offset_x:state(p,'HoleOffsetX'),hole_offset_y:state(p,'HoleOffsetY'),reason:'An undocumented tuple requires a matching authoritative Pad.getAll() record; no inferred scaling'});
  if(layer!==ENUMS.Layer.MULTI) checkLayer(layer,stack);
  const x=finite(state(p,'X'),'pad x'), y=finite(state(p,'Y'),'pad y');
  const angle=finite(rotationOverride??state(p,'Rotation'),'pad rotation');
  const [localX,localY]=coordinateSpace==='world'?inversePoint(x,y,c):[x,y];
  const out={padId:pid,net:state(p,'Net')??'',localX,localY,
    local_rotation:coordinateSpace==='world'?(c.layer===ENUMS.Layer.BOTTOM?c.rotation-angle:angle-c.rotation):angle,
    layer, metadata:{pad_number:state(p,'PadNumber'),primitive_id:pid}};
  if(positionProfile==='footprint_source'){
    const native=sourcePad?.payload,nx=finite(native?.centerX,'native pad x'),ny=finite(native?.centerY,'native pad y');
    const a=radians(c.rotation),mx=nx*(c.layer===ENUMS.Layer.BOTTOM?-1:1);
    const expected=coordinateSpace==='world'?[c.x+mx*Math.cos(a)-ny*Math.sin(a),c.y+mx*Math.sin(a)+ny*Math.cos(a)]:[nx,ny];
    if(!coordinateNear(x,expected[0],c.x,c.y,nx,ny,x,y)||!coordinateNear(y,expected[1],c.x,c.y,nx,ny,x,y))fail('UNPROVEN_PAD_POSITION','Native footprint position differs from API world geometry: '+pid);
    const nativeAngle=finite(native.padAngle,'native pad angle'),localAngle=out.local_rotation;
    if(!near(Math.cos(radians(localAngle)),Math.cos(radians(nativeAngle)))||!near(Math.sin(radians(localAngle)),Math.sin(radians(nativeAngle))))fail('UNPROVEN_PAD_POSITION','Native footprint angle differs from API world geometry: '+pid);
    out.localX=nx;out.localY=ny;out.metadata.pad_positions='native_source';
    out.local_rotation=nativeAngle;
  }
  if(dimensionProfile==='footprint_source')Object.assign(out.metadata,{pad_dimensions:'native_source',native_pad_id:sourcePad.id,api_pad:copy(apiRaw)});
  if(raw[0]===ENUMS.PadShape.RECTANGLE && raw.length===4) {
    const radius=finite(raw[3],'pad corner radius');
    if(radius<0||radius>Math.min(raw[1],raw[2])/2)fail('UNSUPPORTED_PAD','Invalid rounded rectangle radius '+pid);
    Object.assign(out,{shape:radius===0?'RECTANGLE':'ROUNDRECT',localW:finite(raw[1],'pad width',true),localH:finite(raw[2],'pad height',true)});
    if(radius)out.radius=radius;
  }
  else if(raw[0]===ENUMS.PadShape.ELLIPSE && raw.length===3 && raw[1]===raw[2]) Object.assign(out,{shape:'CIRCLE',diameter:finite(raw[1],'pad diameter',true)});
  else if([ENUMS.PadShape.ELLIPSE,ENUMS.PadShape.OBLONG].includes(raw[0])&&raw.length===3)Object.assign(out,{shape:raw[0]==='OVAL'?'OVAL':'ELLIPSE',localW:finite(raw[1],'pad width',true),localH:finite(raw[2],'pad height',true)});
  else if(raw[0]===ENUMS.PadShape.POLYGON && raw.length===2) Object.assign(out,{shape:'POLYGON',...polygonSource(raw[1])});
  else fail('UNSUPPORTED_PAD','Supported pad shapes: RECT, ELLIPSE, OVAL, straight POLYGON: '+pid);
  if(hole) {
    const offsetX=finite(state(p,'HoleOffsetX'),'hole offset x'),offsetY=finite(state(p,'HoleOffsetY'),'hole offset y');
    if(slot){
      const native=sourcePad?.payload,shape=native?.hole,rotation=native?.relativeAngle;
      if(shape?.holeType!=='SLOT'||!near(hole[1],Math.min(shape.width,shape.height))||!near(hole[2],Math.max(shape.width,shape.height))||!near(state(p,'HoleRotation'),rotation*Math.PI/180)||!near(offsetX,native.padOffsetX)||!near(offsetY,native.padOffsetY))fail('UNPROVEN_SLOT','Slot needs matching native dimensions, local angle and offsets: '+pid,{primitive_id:pid,raw_hole:copy(hole),raw_rotation:state(p,'HoleRotation')});
      out.hole={type:'SLOT',width:finite(shape.width,'slot width',true),height:finite(shape.height,'slot height',true),rotation:finite(rotation,'slot rotation'),offsetX,offsetY};
    }else{
      const diameter=finite(hole[1],'pad drill',true);
      out.hole=offsetX||offsetY?{type:'ROUND',diameter,rotation:0,offsetX,offsetY}:diameter;
    }
    out.plated=state(p,'Metallization');if(typeof out.plated!=='boolean') fail('UNPROVEN_DRILL','Explicit metallization required');
  }
  return out;
}
async function activeDocument(eda, expected) {
  const doc=await eda.dmt_SelectControl.getCurrentDocumentInfo();
  if(!doc||doc.documentType!==ENUMS.DocumentType.PCB||!doc.uuid) fail('WRONG_DOCUMENT','Active PCB document required');
  if(expected&&doc.uuid!==expected) fail('WRONG_DOCUMENT','Document UUID changed');
  return doc;
}
async function readSource(eda) {
  const source=await eda.sys_FileManager.getDocumentSource();
  if(typeof source!=='string'||!source.trim()) fail('MISSING_SOURCE','Cannot read complete document source');
  return source;
}
async function collect(eda) {
  const objects={};
  for(const [type,suffix] of Object.entries(MODULES)) {
    const api=eda['pcb_Primitive'+suffix];
    if(typeof api?.getAll!=='function') fail('INCOMPLETE_INVENTORY','Missing getAll for '+type);
    objects[type]=array(await api.getAll(),'getAll '+type);
  }
  const pins=[];for(const c of objects.Component) pins.push(...array(await c.getAllPins(),'getAllPins'));
  objects.ComponentPad=pins;
  return objects;
}
function normalizePouredIdentity(records,objects){
  // Observed IPCB_PrimitivePoured aliases its parent's API ID. Native source
  // retains a separate POURED ID; prove it from the record and every region.
  objects.Poured=objects.Poured.map(p=>{
    const apiId=id(p),parentId=state(p,'PourPrimitiveId');
    if(apiId!==parentId)return p;
    const fills=array(state(p,'PourFills'),'actual Poured fills');
    const candidates=records.filter(r=>{
      if(r.type!=='POURED')return false;
      let identity;try{identity=JSON.parse(r.id);}catch(_){return false;}
      return eq(identity,['POURED',parentId])&&Array.isArray(r.payload.pourFill)&&r.payload.pourFill.length===fills.length&&fills.every(f=>{
        const matches=r.payload.pourFill.filter(x=>x.id===f.id);
        return matches.length===1&&matches[0].fill===f.fill&&matches[0].strokeWidth===f.lineWidth&&eq(matches[0].path,f.path.getSourceStrictComplex());
      });
    });
    if(candidates.length!==1)fail('UNPROVEN_POURED_IDENTITY','Poured API parent alias lacks a unique actual native source counterpart');
    const nativeId=candidates[0].id;
    return new Proxy(p,{get(target,key){
      if(key==='getState_PrimitiveId')return ()=>nativeId;
      if(key==='getState_ApiPrimitiveId')return ()=>apiId;
      const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
    }});
  });
}
function proveInventory(records, objects) {
  const seen=new Map(),entries=new Map(),rotations=new Map();
  for(const [type,items] of Object.entries(objects)) for(const p of items) {
    const key=id(p), actual=state(p,'PrimitiveType');
    if(actual!==type && !(type==='Pad'&&actual==='ComponentPad')) fail('API_TYPE_MISMATCH',key+' expected '+type+' got '+actual);
    if(entries.has(key)){
      const previous=entries.get(key),alias=new Set([type,previous.type]);
      if(alias.size!==2||!alias.has('Pad')||!alias.has('ComponentPad'))fail('DUPLICATE_PRIMITIVE',key);
      const pad=type==='Pad'?p:previous.p,pin=type==='ComponentPad'?p:previous.p;
      for(const field of PAD_ALIAS_FIELDS){
        const a=state(pad,field),b=state(pin,field);
        if(eq(a,b))continue;
        // Observed getAllPins() quantizes world X/Y to 0.1 mil; keep the full Pad.getAll() coordinate.
        if(['X','Y'].includes(field)&&Number.isFinite(a)&&Number.isFinite(b)){
          // A transformed double can lie a few ULPs either side of a decimal half-step.
          // Test only that roundoff interval; never allow an arbitrary 0.1 mil mismatch.
          const roundoff=Number.EPSILON*Math.max(1,Math.abs(a),Math.abs(b))*16;
          if(roundoff<0.05&&[a,a-roundoff,a+roundoff].some(v=>b===Number(v.toFixed(1))))continue;
        }
        // Measured global Pad angles are radians while ComponentPad angles are degrees.
        if(field==='Rotation'&&near(a,radians(b))){rotations.set(key,a*180/Math.PI);continue;}
        // Only this paired-interface ROUND alias has measured 10:1 units. Never scale a lone getter.
        if(field==='Hole'&&a?.[0]==='ROUND'&&a.length===2&&b?.[0]==='ROUND'&&b.length===3&&b[1]>0&&b[1]===b[2]&&near(a[1],b[1]*10))continue;
        if(field==='Hole'&&a?.[0]==='SLOT'&&a.length===3&&b?.[0]==='SLOT'&&b.length===3&&b[1]>0&&b[2]>=b[1]&&near(a[1],b[1]*10)&&near(a[2],b[2]*10))continue;
        fail('PAD_ALIAS_MISMATCH','Pad aliases disagree: '+key+' field '+field,{primitive_id:key,field,global_pad_value:a,component_pin_value:b});
      }
      const parent=previous.p.getState_ParentComponentPrimitiveId;
      if(typeof parent==='function'&&typeof p.getState_ParentComponentPrimitiveId==='function'&&parent.call(previous.p)!==p.getState_ParentComponentPrimitiveId())fail('PAD_ALIAS_MISMATCH','Pad aliases disagree: '+key+' field ParentComponentPrimitiveId',{primitive_id:key,field:'ParentComponentPrimitiveId'});
    }
    entries.set(key,{p,type});seen.set(key,type==='ComponentPad'?'ComponentPad':actual);
  }
  for(const r of records) if(SOURCE_TYPES[r.type]) {
    const expected=SOURCE_TYPES[r.type], actual=seen.get(r.id);
    if(actual!==expected&&!(expected==='Pad'&&actual==='ComponentPad')) fail('INCOMPLETE_INVENTORY','Source primitive absent from API inventory: '+r.type+' '+r.id);
    if(r.payload.unusedInnerLayers?.length) fail('UNSUPPORTED_LAYER_PAD','Removed inner pads '+r.id);
  }
  const authority=new Map(objects.Pad.map(p=>[id(p),p]));
  objects.ComponentPad=objects.ComponentPad.map(pin=>{
    const pad=authority.get(id(pin));if(!pad)return pin;
    const getters=Object.fromEntries([...PAD_ALIAS_FIELDS,'PrimitiveId'].map(field=>['getState_'+field,()=>field==='Rotation'&&rotations.has(id(pin))?rotations.get(id(pin)):state(pad,field)]));
    return {...getters,getState_PrimitiveType:()=> 'ComponentPad',getState_ParentComponentPrimitiveId:()=>state(pin,'ParentComponentPrimitiveId')};
  });
  return seen;
}
function standalonePadRotation(p,sourcePad){
  const raw=finite(state(p,'Rotation'),'standalone pad rotation'),native=sourcePad?.payload.padAngle;
  if(typeof native==='number'&&Number.isFinite(native)){
    if(raw===native)return raw;
    if(near(raw,radians(native)))return raw*180/Math.PI;
    fail('SOURCE_GEOMETRY_MISMATCH','Standalone pad rotation differs from native source: '+id(p),{raw_rotation:raw,native_pad_angle:native});
  }
  if(raw===0)return 0;
  fail('UNPROVEN_ROTATION','A nonzero standalone Pad angle requires a native PAD padAngle: '+id(p),{raw_rotation:raw});
}
function proveFootprintIdentity(records,uuid,cid){
  const blocks=[];
  for(const record of records){
    if(record.type==='DOCHEAD')blocks.push([]);
    if(!blocks.length||record.type==='DOCTYPE')fail('SOURCE_FOOTPRINT_MISMATCH','Native footprint requires explicit DOCHEAD blocks: '+cid);
    blocks.at(-1).push(record);
  }
  if(!uuid||!blocks.length||blocks.some(block=>block[0].payload.docType!=='FOOTPRINT'||block[0].payload.uuid!==uuid))fail('SOURCE_FOOTPRINT_MISMATCH','Live footprint UUID differs from native footprint source: '+cid);
  const geometry=blocks.filter(block=>block.some(r=>SOURCE_TYPES[r.type]||SOURCE_ONLY_TYPES.has(r.type)));
  if(geometry.length!==1||blocks.some(block=>block!==geometry[0]&&block.some(r=>r.type!=='DOCHEAD'&&r.type!=='META')))fail('SOURCE_FOOTPRINT_MISMATCH','Exactly one geometry block plus same-identity DOCHEAD/META-only blocks required: '+cid);
}
/** 入参为完整板框 LINE 清单及同一来源的记录；返回板框与编码证据。
 * 源码端点必须自己精确闭合，API 仅用于逐 ID 互证；开口、分叉及不匹配抛 AdapterError。
 */
function outlineFromLines(lines,records) {
  if(!lines.length) fail('MISSING_OUTLINE','Straight board outline or authoritative board sidecar required');
  const fields=[['StartX','startX'],['StartY','startY'],['EndX','endX'],['EndY','endY']];
  const native=lines.map(p=>records.filter(r=>r.type==='LINE'&&r.id===id(p)));
  const sourceComplete=native.every(rs=>rs.length===1&&rs[0].payload.layerId===ENUMS.Layer.BOARD_OUTLINE&&fields.every(([,f])=>Number.isFinite(rs[0].payload[f])));
  const proofs=[];
  const segments=lines.map((p,i)=>{
    const values=fields.map(([getter,field])=>{
      const api=state(p,getter);
      if(!sourceComplete)return api;
      const value=native[i][0].payload[field];
      if(!nativeSerializedCoordinateMatches(api,value))fail('SOURCE_GEOMETRY_MISMATCH','Board outline source/getter endpoint differs: '+id(p),{field,api,native_source:value});
      if(!coordinateNear(api,value))proofs.push({id:id(p),field,api,native_source:value,error_mil:Math.abs(api-value),serialization_decimals:4});
      return value;
    });
    return [values.slice(0,2),values.slice(2)];
  });
  const rings=[];
  while(segments.length) {
    const [a,b]=segments.pop(), points=[a,b];
    while(!eq(points.at(-1),points[0])) {
      const matches=segments.map((s,i)=>({s,i})).filter(({s})=>eq(s[0],points.at(-1))||eq(s[1],points.at(-1)));
      if(matches.length!==1) fail('AMBIGUOUS_OUTLINE','Open, branched, or duplicated board outline');
      const {s,i}=matches[0];points.push(eq(s[0],points.at(-1))?s[1]:s[0]);segments.splice(i,1);
    }
    rings.push(ring(points));
  }
  if(rings.length!==1) fail('UNSUPPORTED_OUTLINE','Multiple loops require explicit board sidecar with outline/cutouts');
  return {board_bbox:{outline:rings[0],cutouts:[]},proofs};
}
/** 入参为 API/封装源码尺寸及显式尺寸配置；返回能否互证，无效数返回 false。
 * 仅 footprint_source 模式允许已观测到的整数 mil 编码；不会猜测缩放比例。
 */
export function nativeViaDimensionMatches(apiValue,nativeValue,profile){
  if(!Number.isFinite(apiValue)||!Number.isFinite(nativeValue)||apiValue<=0||nativeValue<=0)return false;
  return near(apiValue,nativeValue)||(profile==='footprint_source'&&apiValue===Math.round(nativeValue));
}
function validateFootprintEvidence(evidence, count, stack, component, objects, footprintVias, dimensionProfile) {
  if(!evidence||typeof evidence.footprint_source!=='string'||!evidence.provenance) fail('MISSING_FOOTPRINT_EVIDENCE','Real footprint source, provenance and body polygon required');
  const records=inventorySource(evidence.footprint_source);
  if(records.some(r=>SOURCE_ONLY_TYPES.has(r.type)))fail('UNSUPPORTED_FOOTPRINT_COPPER','Source-only copper inside a footprint is not supported');
  const pads=records.filter(r=>r.type==='PAD');
  if(pads.length!==count) fail('FOOTPRINT_PAD_MISMATCH','Footprint source pad count differs from API pins');
  if(pads.some(r=>r.payload.unusedInnerLayers?.length))fail('UNSUPPORTED_LAYER_PAD','Footprint removes copper from inner pad layers');
  for(const r of records) if(SOURCE_TYPES[r.type]&&r.type!=='PAD') {
    if(r.type==='VIA'){
      const v=r.payload,pid=component.id+r.id,primitive=objects.Via.find(p=>id(p)===pid);
      if(!primitive||v.legacy||v.viaType!=='NORMAL'||v.unusedInnerLayers?.length||v.ruleName||!(v.holeDiameter>0&&v.viaDiameter>v.holeDiameter))fail('UNSUPPORTED_FOOTPRINT_COPPER','Cannot prove ordinary footprint via '+r.id);
      const angle=radians(component.rotation),x=finite(v.centerX,'footprint via x')*(component.layer===ENUMS.Layer.BOTTOM?-1:1),y=finite(v.centerY,'footprint via y');
      const world=[component.x+x*Math.cos(angle)-y*Math.sin(angle),component.y+x*Math.sin(angle)+y*Math.cos(angle)];
      if(!near(state(primitive,'X'),world[0])||!near(state(primitive,'Y'),world[1])||!nativeViaDimensionMatches(state(primitive,'HoleDiameter'),v.holeDiameter,dimensionProfile)||!nativeViaDimensionMatches(state(primitive,'Diameter'),v.viaDiameter,dimensionProfile)||state(primitive,'ViaType')!==ENUMS.Via.VIA||state(primitive,'DesignRuleBlindViaName')|| (v.netName&&state(primitive,'Net')!==v.netName))fail('FOOTPRINT_VIA_MISMATCH','Footprint via differs from API geometry: '+pid);
      if(footprintVias.has(pid))fail('DUPLICATE_PRIMITIVE','Footprint via has multiple owners: '+pid);
      footprintVias.set(pid,{parent_component_id:component.id,footprint_primitive_id:r.id,source_local_x:v.centerX,source_local_y:v.centerY,source_hole_diameter:v.holeDiameter,source_copper_diameter:v.viaDiameter,via_dimensions:dimensionProfile==='footprint_source'?'native_source':'api',api_hole_diameter:state(primitive,'HoleDiameter'),api_copper_diameter:state(primitive,'Diameter'),provenance:evidence.provenance});
      continue;
    }
    if(r.type==='ATTR'||r.type==='ATTRIBUTE'){
      const visible=r.payload.legacy?[r.payload.legacy[7],r.payload.legacy[8]]:[r.payload.keyVisible,r.payload.valueVisible];
      if(visible.every(v=>v===false||v===0))continue;
    }
    const layer=r.payload.layerId??evidence.primitive_layers?.[r.id];
    if(layer===undefined||stack.includes(layer)||layer===ENUMS.Layer.MULTI||layer===ENUMS.Layer.BOARD_OUTLINE) fail('UNSUPPORTED_FOOTPRINT_COPPER','Cannot prove non-pad footprint geometry '+r.type+' '+r.id);
  }
  return {body:ring(evidence.body_polygon),pads,records};
}

/** Assisted automatic export: API supplies live geometry; sidecar supplies unavailable physical evidence. */
export async function exportBoard(eda, context={}) {
  const dimensionProfile=context.pad_dimensions??'api';
  const positionProfile=context.pad_positions??'api';
  if(!['api','footprint_source'].includes(positionProfile))fail('UNPROVEN_PAD_POSITION','pad_positions must be api or footprint_source');
  const footprintViaMovement=context.footprint_via_movement??'fixed';
  if(!['fixed','rigid'].includes(footprintViaMovement))fail('UNPROVEN_FOOTPRINT_MOVEMENT','footprint_via_movement must be fixed or rigid');
  if(!['api','footprint_source'].includes(dimensionProfile))fail('UNPROVEN_PAD_DIMENSIONS','pad_dimensions must be api or footprint_source');
  if(context.board?.usage===undefined&&/TEST[- ]ONLY/i.test(context.board?.provenance??''))fail('UNPROVEN_BOARD_USAGE','TEST-ONLY provenance requires explicit board.usage=test_only');
  const boardUsage=context.board?.usage??'physical';
  if(!['physical','test_only'].includes(boardUsage))fail('UNPROVEN_BOARD_USAGE','board.usage must be physical or test_only');
  const doc=await activeDocument(eda,context.document_uuid), source=await readSource(eda), records=inventorySource(source);
  const pouredProfile=nativePourProfile(context.native_pour_geometry,doc.uuid,source);
  if(records.filter(r=>r.type==='DOCHEAD'||r.type==='DOCTYPE').length!==1)fail('SOURCE_DOCUMENT_MISMATCH','Export requires exactly one PCB document, not a project log');
  const header=records.find(r=>r.type==='DOCHEAD');
  if(header&&(header.payload.docType!=='PCB'||header.payload.uuid!==doc.uuid)) fail('SOURCE_DOCUMENT_MISMATCH','Source header does not match active PCB');
  const objects=await collect(eda);normalizePouredIdentity(records,objects);
  const seen=proveInventory(records,objects);
  const layers=array(await eda.pcb_Layer.getAllLayers(),'layers');
  const copperType=l=>l.type==='SIGNAL'||l.type==='PLANE'||l.id===ENUMS.Layer.TOP||l.id===ENUMS.Layer.BOTTOM;
  for(const l of layers)if(copperType(l)&&![ENUMS.LayerStatus.NOT_USED,ENUMS.LayerStatus.SHOW,ENUMS.LayerStatus.HIDDEN].includes(l.layerStatus))fail('UNPROVEN_LAYER_STATUS','Documented layerStatus required for copper layer '+l.id);
  const enabled=l=>l.layerStatus!==ENUMS.LayerStatus.NOT_USED;
  if(layers.some(l=>l.type==='PLANE'&&enabled(l))) fail('UNSUPPORTED_PLANE','Internal electrical plane requires an explicit adapter');
  const copper=layers.filter(l=>copperType(l)&&enabled(l)).map(l=>l.id);
  const stack=context.copper_layers??(copper.length===2?[ENUMS.Layer.TOP,ENUMS.Layer.BOTTOM]:null);
  if(!Array.isArray(stack)||new Set(stack).size!==stack.length||!eq([...stack].sort((a,b)=>a-b),[...copper].sort((a,b)=>a-b))) fail('UNPROVEN_STACK','Explicit physical copper_layers required for multilayer PCB');
  const knownLayer=n=>{const layer=layers.find(l=>l.id===n);if(!Number.isInteger(n)||!layer)fail('UNPROVEN_LAYER','Primitive layer is missing or unknown: '+n);if(copperType(layer)&&!enabled(layer))fail('UNUSED_COPPER_LAYER','Primitive on unused copper layer: '+n);return n;};
  if(context.pad_coordinates!=='world'&&context.pad_coordinates!=='component_local') fail('UNPROVEN_COORDINATES','Declare pad_coordinates after verifying the runtime coordinate convention');
  const routes={traces:[],vias:[],holes:[],copper_pours:[],keepouts:[]}, components=[],nets={},tags=copy(context.tags??{});
  const inventory=Object.fromEntries(Object.entries(objects).map(([type,items])=>[type,items.map(id)]));
  const teardrops=readTeardrops(records,objects,stack);
  if(pouredProfile)for(const t of teardrops){
    // Local mode has conservative curved-ring geometry. Keep each already
    // proven native-mil TEARDROP path and source record; never use its chord.
    t.filled_polygons=t.filled_polygons.map(r=>({native_compound_path:[copy(r.native_path)]}));
    t.metadata.local_compound_geometry=true;
  }
  routes.copper_pours.push(...teardrops);
  const ignored=[],footprintVias=new Map();
  const endpoint=(compId,p)=>{if(p.net)(nets[p.net]??=[]).push({compId,padId:p.padId});};
  const owned=new Set(objects.ComponentPad.map(id)),globalPads=new Map(objects.Pad.map(p=>[id(p),p]));
  if((dimensionProfile==='footprint_source'||positionProfile==='footprint_source')&&records.some(r=>r.type==='PAD'&&owned.has(r.id)))fail('UNPROVEN_PAD_DIMENSIONS','Owned native PAD override prevents footprint-source geometry proof');
  for(const p of objects.Component) {
    const cid=id(p), c={id:cid,x:finite(state(p,'X'),'component x'),y:finite(state(p,'Y'),'component y'),rotation:finite(state(p,'Rotation'),'component rotation'),layer:state(p,'Layer'),locked:state(p,'PrimitiveLock')};
    c.fixed=c.locked;
    if(c.layer!==ENUMS.Layer.TOP&&c.layer!==ENUMS.Layer.BOTTOM) fail('UNSUPPORTED_COMPONENT_LAYER',cid);
    const pins=objects.ComponentPad.filter(pin=>state(pin,'ParentComponentPrimitiveId')===cid), evidence=context.components?.[cid];
    const footprint=validateFootprintEvidence(evidence,pins.length,stack,c,objects,footprintVias,dimensionProfile);
    const footprintReference=state(p,'Footprint');
    if(dimensionProfile==='footprint_source'||positionProfile==='footprint_source'){
      proveFootprintIdentity(footprint.records,footprintReference?.uuid,cid);
      const instances=records.filter(r=>r.type==='COMPONENT'&&r.id===cid);
      if(instances.length!==1||!instances[0].payload||instances[0].payload.legacy||Object.keys(instances[0].payload).some(k=>!COMPONENT_INSTANCE_FIELDS.has(k)))fail('UNPROVEN_PAD_DIMENSIONS','Unknown or missing component instance fields prevent native dimension proof: '+cid);
    }
    c.body_polygon=footprint.body;
    if(evidence.courtyard_polygon)c.courtyard_polygon=ring(evidence.courtyard_polygon);
    c.pads=pins.map(pin=>{
      if((dimensionProfile==='footprint_source'||positionProfile==='footprint_source')&&!globalPads.has(id(pin)))fail('UNPROVEN_PAD_DIMENSIONS','Global Pad authority required for native footprint geometry proof: '+id(pin));
      return padGeometry(pin,c,context.pad_coordinates,stack,footprint.pads.find(r=>c.id+r.id===id(pin)),undefined,dimensionProfile,positionProfile);
    });
    const attached=[...footprintVias].filter(([,v])=>v.parent_component_id===cid).map(([pid])=>pid);
    if(dimensionProfile==='footprint_source'&&records.some(r=>r.type==='VIA'&&attached.includes(r.id)))fail('UNPROVEN_VIA_DIMENSIONS','Owned native VIA override prevents footprint-source geometry proof: '+cid);
    if(attached.length){
      const fixed=footprintViaMovement!=='rigid'||attached.some(pid=>state(objects.Via.find(v=>id(v)===pid),'PrimitiveLock'));
      c.fixed=c.fixed||fixed;c.metadata={footprint_vias_fixed:fixed,footprint_via_movement:footprintViaMovement,attached_via_ids:attached};
      if(fixed)c.metadata.fixed_reason='Attached footprint vias remain fixed without the verified rigid movement profile';
    }
    const attachedTeardrops=teardrops.filter(t=>t.metadata.refs.some(ref=>pins.some(pin=>id(pin)===ref)||attached.includes(ref))).map(t=>t.id);
    if(attachedTeardrops.length){c.fixed=true;c.metadata={...c.metadata,native_teardrops_fixed:true,attached_teardrop_ids:attachedTeardrops,fixed_reason:'Native teardrop reference geometry cannot be moved independently'};}
    c.metadata={...c.metadata,designator:state(p,'Designator'),footprint:footprintReference,geometry_provenance:evidence.provenance};
    for(const pad of c.pads)endpoint(cid,pad);
    components.push(c);
  }
  for(const p of objects.Pad) if(!owned.has(id(p))) {
    const sourcePad=records.find(r=>r.type==='PAD'&&r.id===id(p)),angle=standalonePadRotation(p,sourcePad);
    const cid='@pad:'+id(p),c={id:cid,x:state(p,'X'),y:state(p,'Y'),rotation:angle,layer:state(p,'Layer')===2?2:1,locked:true,fixed:true};
    const pad=padGeometry(p,c,'world',stack,sourcePad,angle,dimensionProfile);
    const bounds=pad.shape==='POLYGON'?[Math.max(...pad.points.map(v=>Math.abs(v[0])))*2,Math.max(...pad.points.map(v=>Math.abs(v[1])))*2]:[pad.localW??pad.diameter,pad.localH??pad.diameter];
    Object.assign(c,{width:bounds[0],height:bounds[1],pads:[pad],metadata:{synthetic_free_pad:true,body_is_conservative_pad_envelope:true}});
    components.push(c);endpoint(cid,pad);
  }
  for(const c of components)if(c.fixed){
    const previous=tags.component_tags?.[c.id]??{},pose={x:c.x,y:c.y,rotation:c.rotation,layer:c.layer};
    if(previous.fixed_pose&&!Object.keys(pose).every(k=>previous.fixed_pose[k]===pose[k]))fail('FIXED_POSE_MISMATCH','Sidecar fixed pose differs from live geometry: '+c.id);
    (tags.component_tags??={})[c.id]={...previous,fixed:true,fixed_pose:pose};
  }
  const outline=[];
  for(const p of objects.Line) {
    const layer=knownLayer(state(p,'Layer'));
    if(layer===ENUMS.Layer.BOARD_OUTLINE){outline.push(p);continue;}
    if(!copperLayer(layer,stack)){ignored.push({id:id(p),type:'Line',reason:'non-copper layer',layer});continue;}
    routes.traces.push({id:id(p),locked:state(p,'PrimitiveLock'),net:state(p,'Net'),layer,sx:finite(state(p,'StartX'),'trace start x'),sy:finite(state(p,'StartY'),'trace start y'),ex:finite(state(p,'EndX'),'trace end x'),ey:finite(state(p,'EndY'),'trace end y'),width:finite(state(p,'LineWidth'),'trace width',true)});
  }
  for(const p of objects.Via) {
    if(state(p,'ViaType')!==ENUMS.Via.VIA||state(p,'DesignRuleBlindViaName')) fail('UNSUPPORTED_VIA','Only documented ordinary through vias supported: '+id(p));
    const attached=footprintVias.get(id(p));
    const hole=finite(attached?.via_dimensions==='native_source'?attached.source_hole_diameter:state(p,'HoleDiameter'),'via hole',true),diameter=finite(attached?.via_dimensions==='native_source'?attached.source_copper_diameter:state(p,'Diameter'),'via diameter',true);
    if(diameter<=hole)fail('INVALID_VIA','Via has no positive copper annulus '+id(p));
    const via={id:id(p),locked:state(p,'PrimitiveLock'),net:state(p,'Net'),x:finite(state(p,'X'),'via x'),y:finite(state(p,'Y'),'via y'),hole,diameter,layers:[...stack],...(footprintVias.has(id(p))?{metadata:footprintVias.get(id(p))}:{})};
    if(footprintViaMovement==='rigid'&&via.metadata?.parent_component_id){
      const c=components.find(c=>c.id===via.metadata.parent_component_id),a=radians(c.rotation),x=via.metadata.source_local_x*(c.layer===ENUMS.Layer.BOTTOM?-1:1),y=via.metadata.source_local_y;
      // Source/API correspondence was proved above; retain its exact rigid geometry.
      via.x=c.x+x*Math.cos(a)-y*Math.sin(a);via.y=c.y+x*Math.sin(a)+y*Math.cos(a);
    }
    routes.vias.push(via);
  }
  const pours=new Map(objects.Pour.map(p=>[id(p),p]));
  const pourFilled=new Set();
  for(const p of objects.Fill) {
    const layer=knownLayer(state(p,'Layer'));
    if(!copperLayer(layer,stack)){ignored.push({id:id(p),type:'Fill',reason:'non-copper layer',layer});continue;}
    if(state(p,'FillMode')!==ENUMS.Fill.SOLID||state(p,'LineWidth')!==0) fail('UNSUPPORTED_FILL','Only zero-stroke solid Fill supported: '+id(p));
    const region=polygonSource(state(p,'ComplexPolygon').getSource());
    const sourceRecord=records.find(r=>r.type==='FILL'&&r.id===id(p));
    if(sourceRecord?.payload.path&&stable(canonicalRegion(region))!==stable(canonicalRegion(polygonSource(sourceRecord.payload.path))))fail('SOURCE_GEOMETRY_MISMATCH','Fill API and native source path differ '+id(p));
    routes.copper_pours.push({id:id(p),locked:state(p,'PrimitiveLock'),source_kind:'Fill',net:state(p,'Net')??'',layer,filled_polygons:[region]});
  }
  for(const p of objects.Poured) {
    const parentId=state(p,'PourPrimitiveId'), parent=pours.get(parentId);
    if(!parent) fail('ORPHAN_POUR','Poured has no parent '+parentId);
    const sourcePoured=records.filter(r=>r.type==='POURED'&&r.id===id(p));
    const rawRegions=[];
    const regions=array(state(p,'PourFills'),'pour fills').map(f=>{
      if(pouredProfile){
        if(sourcePoured.length!==1)fail('UNPROVEN_POUR_COORDINATES','Unique native Poured source record required');
        const matching=sourcePoured[0].payload.pourFill?.filter(r=>r.id===f.id);
        if(matching?.length!==1||matching[0].fill!==f.fill||matching[0].strokeWidth!==f.lineWidth)fail('SOURCE_GEOMETRY_MISMATCH','Actual Poured fill/stroke differs from source');
        const raw=copy(f.path.getSourceStrictComplex());
        if(stable(raw)!==stable(matching[0].path))fail('SOURCE_GEOMETRY_MISMATCH','Actual Poured path differs from source');
        rawRegions.push({id:f.id,path:copy(raw),fill:f.fill,lineWidth:f.lineWidth});
        if(f.fill===true&&f.lineWidth===0)return {native_compound_path:nativeCompoundPaths(raw,pouredProfile.coordinate_scale)};
        // 热辐条是实际导电铜；长度和宽度遵循同一 POURED 长度单位，绝不丢弃。
        if(f.fill===false&&f.lineWidth>0)return nativeStraightStroke(raw,f.lineWidth,pouredProfile.coordinate_scale);
        fail('UNSUPPORTED_FILL','Unsupported filled/stroked Poured region '+id(p));
      }
      if(f.fill!==true||f.lineWidth!==0) fail('UNSUPPORTED_FILL','Uncalibrated stroked Poured '+id(p));
      return polygonSource(f.path.getSource());
    });
    if(!regions.length)fail('UNFILLED_POUR',parentId);
    if(pouredProfile&&sourcePoured[0].payload.pourFill.length!==regions.length)fail('INCOMPLETE_INVENTORY','Actual Poured region count differs from native source');
    routes.copper_pours.push({id:id(p),locked:state(parent,'PrimitiveLock'),source_kind:'Poured',parent_id:parentId,net:state(parent,'Net'),layer:checkLayer(state(parent,'Layer'),stack),filled_polygons:regions,
      ...(pouredProfile?{metadata:{native_poured_regions:rawRegions,native_source_record:copy(sourcePoured[0]),api_primitive_id:typeof p.getState_ApiPrimitiveId==='function'?p.getState_ApiPrimitiveId():id(p)}}:{})});
    pourFilled.add(parentId);
  }
  for(const [pid] of pours) if(!pourFilled.has(pid)) fail('UNFILLED_POUR','Border has no actual filled copper: '+pid);
  for(const type of ['Arc','Polyline','Region','String','Attribute','Dimension','Image','Object']) for(const p of objects[type]) {
    if(type==='Attribute'&&state(p,'KeyVisible')===false&&state(p,'ValueVisible')===false){ignored.push({id:id(p),type,reason:'both attribute visibility flags are false'});continue;}
    const layer=knownLayer(state(p,'Layer'));
    if(type==='Region'||copperLayer(layer,stack)||layer===ENUMS.Layer.MULTI) fail('UNSUPPORTED_GEOMETRY',type+' '+id(p)+' affects copper/constraints');
    if(layer===ENUMS.Layer.BOARD_OUTLINE&&!context.board) fail('UNSUPPORTED_OUTLINE',type+' on board outline needs authoritative board sidecar');
    ignored.push({id:id(p),type,reason:layer===ENUMS.Layer.BOARD_OUTLINE?'authoritative board sidecar':'non-copper layer',layer});
  }
  let board_bbox,outlineProofs=[];
  if(context.board){
    if(!context.board.provenance)fail('MISSING_BOARD_EVIDENCE','Board sidecar provenance required');
    board_bbox={outline:ring(context.board.outline),cutouts:array(context.board.cutouts??[],'cutouts').map(ring)};
  } else {const resolved=outlineFromLines(outline,records);board_bbox=resolved.board_bbox;outlineProofs=resolved.proofs;}
  const end=await activeDocument(eda,doc.uuid),sourceAfter=await readSource(eda);
  if(sourceFingerprint(sourceAfter)!==sourceFingerprint(source))fail('BOARD_CHANGED','Document changed during export; discard snapshot');
  return {units:'mil',board_bbox,components,nets,tags,routes,rules:{...context.rules,copper_layers:[...stack]},metadata:{
    source:{document_uuid:end.uuid,fingerprint:sourceFingerprint(source),...(boardUsage==='test_only'?{usage:'test_only'}:{})},board_usage:boardUsage, source_fingerprint_scheme:'dochead-volatile-v1',export_mode:'api_with_evidence_sidecar',
    inventory, source_record_count:records.length, total_api_primitives:seen.size, ignored,
    source_only_inventory:{Teardrop:teardrops.map(t=>t.id)},
    ...(outlineProofs.length?{outline_endpoint_encoding_proofs:outlineProofs}:{}),
    ...(pouredProfile?{native_pour_geometry:pouredProfile}:{}),
    pad_coordinates:context.pad_coordinates,pad_dimensions:dimensionProfile,pad_positions:positionProfile,footprint_via_movement:footprintViaMovement, api_reference_version:'1.1.36', live_board_validated:false,
  }};
}

export async function runNativeDrc(eda, documentUuid) {
  await activeDocument(eda,documentUuid);
  const issues=await eda.pcb_Drc.check(true,false,true);
  if(!Array.isArray(issues)) fail('INVALID_DRC_RESPONSE','Verbose native DRC must return an array');
  await activeDocument(eda,documentUuid);
  return {passed:issues.length===0,issues:copy(issues),strict:true,verbose:true};
}

function encodeBase64(bytes) {
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/',out=[];
  let chunk='';
  for(let i=0;i<bytes.length;i+=3){
    const n=bytes[i]<<16|(bytes[i+1]??0)<<8|(bytes[i+2]??0);
    chunk+=alphabet[n>>>18]+alphabet[n>>>12&63]+(i+1<bytes.length?alphabet[n>>>6&63]:'=')+(i+2<bytes.length?alphabet[n&63]:'=');
    if(chunk.length>=16384){out.push(chunk);chunk='';}
  }
  out.push(chunk);
  return out.join('');
}

/** Native handoff files, gated by zero native DRC issues; no manufacturing signoff. */
export async function exportManufacturing(eda,opts={}) {
  let drc=null;
  try {
    strictKeys(opts,['source','package_kind','max_file_bytes','context'],'manufacturing options');
    strictKeys(opts.source,['document_uuid','fingerprint','usage'],'manufacturing source');
    if(opts.source.usage==='test_only'||opts.context?.board?.usage==='test_only')fail('TEST_ONLY_GEOMETRY','A test-only boundary cannot authorize manufacturing');
    if(opts.source.usage!==undefined&&opts.source.usage!=='physical')fail('UNPROVEN_BOARD_USAGE','Unknown source usage');
    const source=opts.source,uuid=ident(source.document_uuid,'document_uuid');
    if(!/^sha256:[0-9a-f]{64}$/.test(source.fingerprint))fail('MISSING_LIVE_SOURCE','Manufacturing needs the current native source fingerprint');
    const packageKind=opts.package_kind??'gerber',limit=opts.max_file_bytes??33554432;
    if(!['gerber','manufacture_data'].includes(packageKind))fail('INVALID_OPTIONS','package_kind must be gerber or manufacture_data');
    if(!Number.isInteger(limit)||limit<=0||limit>67108864)fail('INVALID_OPTIONS','max_file_bytes must be 1..67108864');
    const stableSource=async()=>{await activeDocument(eda,uuid);if(sourceFingerprint(await readSource(eda))!==source.fingerprint)fail('STALE_SOURCE','Native document source changed during manufacturing export');};
    await stableSource();
    const api=eda.pcb_ManufactureData,packageMethod=packageKind==='gerber'?'getGerberFile':'getManufactureData';
    for(const name of [packageMethod,'getBomFile','getPickAndPlaceFile'])if(typeof api?.[name]!=='function')fail('MISSING_API','pcb_ManufactureData.'+name);
    drc=await runNativeDrc(eda,uuid);
    if(!drc.passed)fail('NATIVE_DRC_FAILED','Native DRC has issues; no manufacturing files requested');
    await stableSource();
    const requests=[['fabrication',()=>api[packageMethod]()],['bom',()=>api.getBomFile('BOM','csv')],['pick_and_place',()=>api.getPickAndPlaceFile('PickAndPlace','csv',ENUMS.Unit.MILLIMETER)]];
    const files=[];
    for(const [kind,request] of requests){
      await stableSource();
      const file=await request();
      if(!file||typeof file.arrayBuffer!=='function'||typeof file.name!=='string'||!file.name||!Number.isInteger(file.size)||file.size<=0)fail('EMPTY_MANUFACTURING_FILE','Native API returned no nonempty File for '+kind);
      if(file.size>limit)fail('MANUFACTURING_FILE_TOO_LARGE',kind+' exceeds max_file_bytes');
      const bytes=new Uint8Array(await file.arrayBuffer());
      if(bytes.byteLength!==file.size)fail('MANUFACTURING_SIZE_MISMATCH','File byte count differs from File.size: '+kind);
      files.push({kind,name:file.name,mime_type:file.type??'',size:bytes.length,sha256:hashBytes(bytes),data_base64:encodeBase64(bytes)});
      await stableSource();
    }
    return {success:true,source:copy(source),package_kind:packageKind,drc,files,
      scope:'native manufacturing handoff files with zero native DRC issues; no EMC or manufacturing signoff',
      settings:'native fabrication defaults/frontend configuration; BOM CSV default columns; pick-and-place CSV in mm',live_board_validated:false};
  }catch(error){return {success:false,files:[],drc,error:{code:error.code??'API_ERROR',message:error.message,details:error.details??{}},live_board_validated:false};}
}

function strictKeys(obj, allowed, label) {
  if(!obj||typeof obj!=='object'||Array.isArray(obj)||Object.keys(obj).some(k=>!allowed.includes(k))) fail('INVALID_PLAN',label+' has unknown keys or is not an object');
}
function validatePlan(plan) {
  strictKeys(plan,['schema_version','plan_id','source','components','add','remove','metadata','target_board'],'plan');
  if(plan.schema_version!==1)fail('INVALID_PLAN','schema_version must be 1');
  ident(plan.plan_id,'plan_id');
  strictKeys(plan.source,['document_uuid','fingerprint','usage'],'source');
  for(const [usage,label] of [[plan.source.usage,'source'],[plan.metadata?.board_usage,'plan metadata'],[plan.metadata?.source?.usage,'plan metadata source'],[plan.target_board?.metadata?.board_usage,'target board'],[plan.target_board?.metadata?.source?.usage,'target source']]){
    if(usage==='test_only')fail('TEST_ONLY_GEOMETRY','A test-only preview cannot be applied: '+label);
    if(usage!==undefined&&usage!=='physical')fail('UNPROVEN_BOARD_USAGE','Unknown board usage: '+label);
  }
  if(plan.metadata?.preview_only===true||plan.target_board?.metadata?.preview_only===true)fail('TEST_ONLY_GEOMETRY','A test-only preview cannot be applied');
  if(!plan.source.document_uuid||!plan.source.fingerprint)fail('MISSING_LIVE_SOURCE','A live EasyEDA export with document_uuid and fingerprint is required; offline plans are preview only');
  ident(plan.source.document_uuid,'document_uuid');
  if(!/^sha256:[0-9a-f]{64}$/.test(plan.source.fingerprint)) fail('INVALID_PLAN','SHA-256 source fingerprint required');
  strictKeys(plan.add,['traces','vias','copper_pours'],'add');strictKeys(plan.remove,['traces','vias','copper_pours'],'remove');
  for(const bucket of ['traces','vias','copper_pours']) {array(plan.add[bucket], 'add.'+bucket);array(plan.remove[bucket], 'remove.'+bucket).forEach(v=>ident(v,'remove ID'));}
  array(plan.components,'components');
  if(new Set(plan.components.map(c=>c.id)).size!==plan.components.length) fail('INVALID_PLAN','Repeated component id');
}
const stable = value => {
  if(Array.isArray(value))return '['+value.map(stable).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stable(value[k])).join(',')+'}';
  if(typeof value==='number')return JSON.stringify(Math.round(value*1e8)/1e8);
  return JSON.stringify(value);
};
function canonicalRing(points) {
  const vertices=ring(points), representations=[];
  for(const order of [vertices,[...vertices].reverse()]) for(let i=0;i<order.length;i++) representations.push(JSON.stringify([...order.slice(i),...order.slice(0,i)]));
  return representations.sort()[0];
}
function canonicalRegion(r){
  if(r.native_compound_path!==undefined)fail('LOCAL_PREVIEW_ONLY','Calibrated native compound copper is supported only by local planning/preview, not strict transactions');
  if(r.native_path!==undefined&&r.holes!==undefined&&(!Array.isArray(r.holes)||r.holes.length))fail('UNSUPPORTED_NATIVE_PATH','Native curved regions cannot contain holes');
  const holes=(r.holes??[]).map(canonicalRing).sort();
  if(r.native_path===undefined)return {points:canonicalRing(r.points),holes};
  const native=nativePathRegion(r.native_path);
  if(!eq(ring(r.points),native.points))fail('NATIVE_PATH_POINTS_MISMATCH','Preview points differ from native path endpoints');
  if(!native.segments.some(s=>s.angle))return {points:canonicalRing(native.points),holes};
  const forward=native.segments,reverse=[...forward].reverse().map(s=>({start:s.end,end:s.start,angle:-s.angle})),forms=[];
  for(const order of [forward,reverse])for(let i=0;i<order.length;i++)forms.push(JSON.stringify([...order.slice(i),...order.slice(0,i)].map(s=>[...s.start,...s.end,s.angle])));
  return {native_path:forms.sort()[0],holes};
}
const sorted = values => values.map(stable).sort();
/** Geometry-only comparison: newly assigned native IDs are intentionally excluded. */
export function boardGeometrySignature(board) {
  const components=array(board.components,'target components').map(c=>({id:c.id,x:c.x,y:c.y,rotation:c.rotation,layer:c.layer,
    body:c.body_polygon?canonicalRing(c.body_polygon):[c.width,c.height],
    pads:sorted(c.pads.map(p=>({id:p.padId,net:p.net??'',x:p.localX,y:p.localY,rotation:p.local_rotation??0,layer:p.layer,layers:p.layers,
      shape:p.shape,w:p.shape==='CIRCLE'?undefined:p.localW??p.width,h:p.shape==='CIRCLE'?undefined:p.localH??p.height,diameter:p.shape==='CIRCLE'?p.diameter??p.localW:p.diameter,radius:p.radius??p.corner_radius,hole:p.hole,plated:p.plated,
      polygon:p.shape==='POLYGON'?canonicalRegion(p):undefined}))),
  }));
  const routes=board.routes??{}, outline=Array.isArray(board.board_bbox)?{
    outline:[[board.board_bbox[0],board.board_bbox[1]],[board.board_bbox[2],board.board_bbox[1]],[board.board_bbox[2],board.board_bbox[3]],[board.board_bbox[0],board.board_bbox[3]]],cutouts:[]}:board.board_bbox;
  const signature={components:sorted(components),board:canonicalRegion({points:outline.outline,holes:outline.cutouts}),
    traces:sorted((routes.traces??[]).map(t=>({net:t.net,layer:t.layer,endpoints:sorted([[t.sx,t.sy],[t.ex,t.ey]]),width:t.width}))),
    vias:sorted([...(routes.vias??[]),...(routes.shield_vias??[])].map(v=>({net:v.net,x:v.x,y:v.y,hole:v.hole,diameter:v.diameter,layers:v.layers??(v.layer===12?board.rules?.copper_layers:undefined)}))),
    fills:sorted((routes.copper_pours??[]).flatMap(p=>p.filled_polygons.map(r=>({net:p.net,layer:p.layer,...canonicalRegion(r)})))),
    holes:sorted((routes.holes??[]).map(h=>({x:h.x,y:h.y,diameter:h.diameter,layers:h.layers}))),
  };
  return fingerprint(stable(signature));
}
const sharedRuntime=globalThis.__pcb_pipeline_adapter_runtime_v1??={version:1,inflight:new Set(),dispatchingJobs:new Set()};
if(sharedRuntime.version!==1||!(sharedRuntime.inflight instanceof Set)||!(sharedRuntime.dispatchingJobs instanceof Set))fail('INVALID_RUNTIME_STATE','Shared adapter lock state is incompatible');
const {inflight,dispatchingJobs}=sharedRuntime;
const APPLY_JOB_PREFIX='pcb_pipeline:apply_job:';
function applyJobKey(key){
  if(typeof key!=='string'||!/^pcb_pipeline:apply_job:[A-Za-z0-9._:-]{1,160}$/.test(key))fail('INVALID_JOB_KEY','Expected pcb_pipeline:apply_job: plus a unique alphanumeric job suffix');
  return key;
}
function jobFailure(plan,error){
  return {success:false,applied:false,saved:false,plan_id:plan.plan_id,error:{code:error.code??'API_ERROR',message:error.message,details:error.details??{}},drc:null,events:[],rollback:{attempted:false,restored:false},backup_storage_key:null};
}
/** One timer dispatch; a pending job is never automatically dispatched again. */
export async function startApplyJob(eda,plan,opts={},jobKey){
  applyJobKey(jobKey);validatePlan(plan);
  strictKeys(opts,['context','save'],'apply job options');
  if(opts.save!==undefined&&typeof opts.save!=='boolean')fail('INVALID_OPTIONS','save must be boolean');
  if(opts.context?.board?.usage==='test_only')fail('TEST_ONLY_GEOMETRY','A test-only boundary cannot authorize native writes');
  for(const name of ['getExtensionUserConfig','setExtensionUserConfig'])if(typeof eda.sys_Storage?.[name]!=='function')fail('MISSING_API','sys_Storage.'+name);
  if(typeof eda.sys_Timer?.setTimeoutTimer!=='function')fail('MISSING_API','sys_Timer.setTimeoutTimer');
  const uuid=plan.source.document_uuid,activeKey='pcb_pipeline:apply_job_active:'+uuid;
  if(dispatchingJobs.has(uuid))fail('TRANSACTION_BUSY','An apply job is being dispatched for this PCB');
  dispatchingJobs.add(uuid);
  try{
    if(await eda.sys_Storage.getExtensionUserConfig(jobKey)!==undefined)fail('JOB_EXISTS','Existing job keys cannot be overwritten or dispatched again');
    const active=await eda.sys_Storage.getExtensionUserConfig(activeKey);
    if(active){
      const previous=await eda.sys_Storage.getExtensionUserConfig(active.job_key);
      if(!previous||previous.status!=='final')fail('TRANSACTION_BUSY','A pending or unresolved apply job already owns this PCB');
    }
    const savedPlan=copy(plan),savedOptions=copy(opts),timerId='pcb_pipeline:apply_timer:'+jobKey.slice(APPLY_JOB_PREFIX.length);
    let job={schema_version:1,success:true,job_key:jobKey,timer_id:timerId,document_uuid:uuid,plan_id:plan.plan_id,status:'pending',created_at:Date.now(),result:null};
    const persist=async()=>{if(await eda.sys_Storage.setExtensionUserConfig(jobKey,job)!==true)fail('JOB_STORE_FAILED','Cannot persist apply job state');};
    await persist();
    let accepted=false,invoked=false;
    const finish=async result=>{job={...job,status:'final',completed_at:Date.now(),result};await persist();return copy(job);};
    const run=async()=>{
      if(!accepted||invoked)return;
      invoked=true;
      const current=await eda.sys_Storage.getExtensionUserConfig(jobKey);
      if(current?.status!=='pending')return;
      job={...job,started_at:Date.now()};await persist();
      let result;
      try{result=await applyPlan(eda,savedPlan,savedOptions);}catch(error){result=jobFailure(savedPlan,error);}
      await finish(result);
    };
    try{
      if(await eda.sys_Storage.setExtensionUserConfig(activeKey,{job_key:jobKey})!==true)fail('JOB_STORE_FAILED','Cannot persist active apply job owner');
      accepted=eda.sys_Timer.setTimeoutTimer(timerId,1000,()=>{
        // Storage failure leaves the durable job pending: inspect the native journal; never retry apply.
        return run().catch(()=>{});
      })===true;
      if(!accepted)fail('JOB_SCHEDULE_FAILED','Native timeout timer rejected the apply job');
    }catch(error){
      job.success=false;
      return await finish(jobFailure(savedPlan,{code:error.code??'JOB_SCHEDULE_FAILED',message:error.message,details:error.details}));
    }
    return copy(job);
  }finally{dispatchingJobs.delete(uuid);}
}
/** Reads durable pending/final state without rescheduling or touching the PCB. */
export async function readApplyJob(eda,jobKey){
  applyJobKey(jobKey);
  if(typeof eda.sys_Storage?.getExtensionUserConfig!=='function')fail('MISSING_API','sys_Storage.getExtensionUserConfig');
  const job=await eda.sys_Storage.getExtensionUserConfig(jobKey);
  if(job===undefined)return {success:false,job_key:jobKey,status:'missing',error:{code:'JOB_NOT_FOUND',message:'No persistent apply job exists'}};
  if(job?.job_key!==jobKey||!['pending','final'].includes(job.status))fail('INVALID_JOB_STATE','Stored apply job identity or status is invalid');
  if(job.status==='final'&&typeof job.result?.success!=='boolean')fail('INVALID_JOB_STATE','Final job must retain the complete apply result');
  return copy(job);
}
/** Applies a reviewed, verified plan. Native DRC failure triggers source restoration. */
export async function applyPlan(eda, plan, opts={}) {
  validatePlan(plan);
  const uuid=plan.source.document_uuid, key='pcb_pipeline:journal:'+uuid, transaction=uuid+':'+plan.plan_id;
  if(inflight.has(uuid))fail('TRANSACTION_BUSY','Another apply is running for this PCB');
  inflight.add(uuid);
  let original, journal, edited=false, drc=null,generatedNativeCopper;
  const events=[];
  const event=(kind,data={})=>events.push({kind,...data});
  const store=async()=>{
    if(await eda.sys_Storage.setExtensionUserConfig(key,journal)!==true)fail('JOURNAL_FAILED','Could not durably write extension journal');
  };
  try {
    if(opts.context?.board?.usage==='test_only')fail('TEST_ONLY_GEOMETRY','A test-only boundary cannot authorize native writes');
    await activeDocument(eda,uuid);original=await readSource(eda);
    if(sourceFingerprint(original)!==plan.source.fingerprint)fail('STALE_PLAN','Source fingerprint changed; re-export and re-plan');
    const previous=await eda.sys_Storage.getExtensionUserConfig(key);
    if(previous&&['pending','recovery_required'].includes(previous.status))fail('RECOVERY_REQUIRED','Previous transaction needs recovery; inspect its backup before continuing');
    const used=previous?.used_plan_ids??[];
    if(used.includes(plan.plan_id))fail('DUPLICATE_PLAN','Plan has already been attempted');
    const before=await exportBoard(eda,{...opts.context,document_uuid:uuid});
    if(before.metadata.source.fingerprint!==plan.source.fingerprint)fail('STALE_PLAN','Document changed during preflight');
    const stack=before.rules.copper_layers, changes=[], removals=[], adds=[], created=[],attachedTargets=[];
    const originalTeardrops=before.routes.copper_pours.filter(p=>p.source_kind==='Teardrop'),protectedTeardropRefs=new Set(originalTeardrops.flatMap(p=>p.metadata.refs));
    for(const c of plan.components) {
      strictKeys(c,['id','x','y','rotation','layer'],'component move');ident(c.id,'component id');
      finite(c.x,'component x');finite(c.y,'component y');finite(c.rotation,'component rotation');
      if(c.layer!==1&&c.layer!==2)fail('INVALID_PLAN','Component layer must be TOP/BOTTOM');
      if(before.components.find(p=>p.id===c.id)?.metadata?.footprint_vias_fixed)fail('UNSUPPORTED_FOOTPRINT_MOVE','Component has attached footprint vias and remains fixed: '+c.id);
      if(before.components.find(p=>p.id===c.id)?.metadata?.native_teardrops_fixed)fail('UNSUPPORTED_TEARDROP_MOVE','Component has native teardrop references and remains fixed: '+c.id);
      const parent=before.components.find(p=>p.id===c.id),attached=before.routes.vias.filter(v=>v.metadata?.parent_component_id===c.id);
      if(attached.length){
        const declaration=before.tags.component_tags?.[c.id]??{};
        if(parent.metadata.footprint_via_movement!=='rigid'||parent.fixed||parent.locked||declaration.fixed||declaration.fixed_pose)fail('UNSUPPORTED_FOOTPRINT_MOVE','Attached via parent is fixed: '+c.id);
        if(c.layer!==parent.layer)fail('UNSUPPORTED_FOOTPRINT_MOVE','Attached via side changes have not been verified: '+c.id);
        if(!plan.target_board)fail('INVALID_PLAN','Rigid footprint movement requires complete target_board');
        const targetParent=plan.target_board.components.find(p=>p.id===c.id);
        if(!targetParent||targetParent.metadata?.footprint_via_movement!=='rigid'||!eq(targetParent.metadata.attached_via_ids,parent.metadata.attached_via_ids))fail('INVALID_PLAN','Attached via target ownership differs: '+c.id);
        for(const v of attached){
          if(v.locked)fail('UNSUPPORTED_FOOTPRINT_MOVE','Attached via is locked: '+v.id);
          const meta=v.metadata,angle=radians(c.rotation),x=meta.source_local_x*(c.layer===ENUMS.Layer.BOTTOM?-1:1),y=meta.source_local_y;
          const expected={...copy(v),x:c.x+x*Math.cos(angle)-y*Math.sin(angle),y:c.y+x*Math.sin(angle)+y*Math.cos(angle)};
          const target=plan.target_board.routes?.vias?.filter(p=>p.id===v.id)??[];
          if(target.length!==1||stable(target[0])!==stable(expected))fail('INVALID_PLAN','Attached via target is not the exact native rigid transform: '+v.id);
          attachedTargets.push(expected);
        }
      }
      const prim=await eda.pcb_PrimitiveComponent.get(c.id);
      if(!prim||state(prim,'PrimitiveLock'))fail('INVALID_PLAN','Component absent or locked: '+c.id);
      changes.push({prim,change:c});
    }
    for(const [bucket,type] of [['traces','Line'],['vias','Via'],['copper_pours','Fill']]) {
      if(new Set(plan.remove[bucket]).size!==plan.remove[bucket].length)fail('INVALID_PLAN','Repeated remove id');
      for(const pid of plan.remove[bucket]) {
        if(originalTeardrops.some(p=>p.id===pid)||protectedTeardropRefs.has(pid))fail('UNSUPPORTED_TEARDROP_WRITE','Native teardrops and referenced input copper cannot be independently deleted: '+pid);
        if(type==='Via'&&before.routes.vias.find(v=>v.id===pid)?.metadata?.parent_component_id)fail('UNSUPPORTED_FOOTPRINT_WRITE','Cannot independently delete an attached footprint via: '+pid);
        const prim=await eda['pcb_Primitive'+type].get(pid);
        if(!prim||state(prim,'PrimitiveLock'))fail('INVALID_PLAN','Removal absent or locked '+pid);
        if(type!=='Via')checkLayer(state(prim,'Layer'),stack);
        removals.push({type,pid});
      }
    }
    for(const t of plan.add.traces) {
      strictKeys(t,['net','layer','sx','sy','ex','ey','width'],'trace');ident(t.net,'trace net');checkLayer(t.layer,stack);
      for(const f of ['sx','sy','ex','ey'])finite(t[f],f);finite(t.width,'trace width',true);
      adds.push({type:'Line',args:[t.net,t.layer,t.sx,t.sy,t.ex,t.ey,t.width,false]});
    }
    for(const v of plan.add.vias) {
      strictKeys(v,['net','x','y','hole','diameter','layers'],'via');ident(v.net,'via net');finite(v.x,'via x');finite(v.y,'via y');finite(v.hole,'via hole',true);finite(v.diameter,'via diameter',true);
      if(v.diameter<=v.hole||!eq(v.layers,stack))fail('INVALID_PLAN','Only complete-stack through vias with positive annulus supported');
      adds.push({type:'Via',args:[v.net,v.x,v.y,v.hole,v.diameter,ENUMS.Via.VIA,null,null,false]});
    }
    for(const p of plan.add.copper_pours) {
      strictKeys(p,['net','layer','filled_polygons'],'copper pour');ident(p.net,'pour net');checkLayer(p.layer,stack);
      if(!array(p.filled_polygons,'filled_polygons').length)fail('INVALID_PLAN','Empty copper');
      for(const region of p.filled_polygons) {
        if(region.native_path!==undefined)fail('UNSUPPORTED_WRITE_GEOMETRY','Native curved source-only copper cannot be written as Fill');
        strictKeys(region,['points','holes'],'filled region');
        if((region.holes??[]).length)fail('UNSUPPORTED_WRITE_GEOMETRY','Fill API takes one IPCB_Polygon; holes cannot be written safely');
        const points=ring(region.points), src=[...points[0],'L',...points.slice(1).flat()];
        const polygon=eda.pcb_MathPolygon.createPolygon(src);
        if(!polygon)fail('INVALID_POLYGON','Native polygon constructor rejected region');
        adds.push({type:'Fill',args:[p.layer,polygon,p.net,ENUMS.Fill.SOLID,0,false]});
      }
    }
    journal={schema_version:1,transaction,plan_id:plan.plan_id,status:'pending',used_plan_ids:[...used,plan.plan_id],document_uuid:uuid,source_fingerprint:plan.source.fingerprint,backup_source:original,plan:copy(plan),events};
    await store();
    // Re-read immediately before the first mutation, after durable backup.
    await activeDocument(eda,uuid);
    if(sourceFingerprint(await readSource(eda))!==sourceFingerprint(original))fail('STALE_PLAN','Document changed after backup');
    for(const {prim,change:c} of changes) {
      await activeDocument(eda,uuid);edited=true;
      const a=prim.toAsync();a.setState_X(c.x);a.setState_Y(c.y);a.setState_Rotation(c.rotation);a.setState_Layer(c.layer);
      if(!await a.done())fail('API_WRITE_FAILED','Component done() failed '+c.id);
      event('move',{id:c.id});await store();
    }
    for(const {type,pid} of removals) {
      await activeDocument(eda,uuid);edited=true;
      if(await eda['pcb_Primitive'+type].delete(pid)!==true)fail('API_WRITE_FAILED','delete failed '+pid);
      event('remove',{type,id:pid});await store();
    }
    for(const {type,args} of adds) {
      await activeDocument(eda,uuid);edited=true;
      const p=await eda['pcb_Primitive'+type].create(...args);
      if(!p)fail('API_WRITE_FAILED','create failed '+type);
      created.push({type,id:id(p),args});event('create',{type,id:id(p)});await store();
    }
    const after=await exportBoard(eda,{...opts.context,document_uuid:uuid});
    for(const c of plan.components) {
      const actual=after.components.find(x=>x.id===c.id);
      if(!actual||!['x','y','rotation','layer'].every(k=>actual[k]===c[k]))fail('READBACK_FAILED','Component readback mismatch '+c.id);
    }
    for(const expected of attachedTargets){
      const actual=after.routes.vias.filter(v=>v.id===expected.id);
      if(actual.length!==1||stable(actual[0])!==stable(expected))fail('READBACK_FAILED','Attached via rigid movement readback mismatch '+expected.id);
    }
    for(const {type,pid} of removals)if(after.metadata.inventory[type].includes(pid))fail('READBACK_FAILED','Deleted primitive still present '+pid);
    for(const e of events.filter(e=>e.kind==='create')){
      if(!after.metadata.inventory[e.type].includes(e.id)){
        const cItem=created.find(c=>c.type===e.type&&c.id===e.id);
        const a=cItem?.args;
        const mergedOk=e.type==='Line'&&a&&traceCoveredByTraces({net:a[0],layer:a[1],sx:a[2],sy:a[3],ex:a[4],ey:a[5],width:a[6]},after.routes.traces);
        if(!mergedOk)fail('READBACK_FAILED','Created primitive absent '+e.id);
      }
    }
    for(const {type,id:pid,args:a} of created) {
      let actual, expected;
      if(type==='Line'){
        const t=after.routes.traces.find(t=>t.id===pid);
        actual=t&&{net:t.net,layer:t.layer,endpoints:sorted([[t.sx,t.sy],[t.ex,t.ey]]),width:t.width};
        expected={net:a[0],layer:a[1],endpoints:sorted([[a[2],a[3]],[a[4],a[5]]]),width:a[6]};
        if(stable(actual)!==stable(expected)){
          const reqSeg={net:a[0],layer:a[1],sx:a[2],sy:a[3],ex:a[4],ey:a[5],width:a[6]};
          if(traceCoveredByTraces(reqSeg,after.routes.traces))continue;
        }
      }else if(type==='Via'){
        const v=after.routes.vias.find(v=>v.id===pid);
        actual=v&&{net:v.net,x:v.x,y:v.y,hole:v.hole,diameter:v.diameter,layers:v.layers};
        expected={net:a[0],x:a[1],y:a[2],hole:a[3],diameter:a[4],layers:stack};
      }else{
        const p=after.routes.copper_pours.find(p=>p.id===pid);
        actual=p&&{net:p.net,layer:p.layer,regions:sorted(p.filled_polygons.map(canonicalRegion))};
        expected={net:a[2],layer:a[0],regions:sorted([canonicalRegion(polygonSource(a[1].getSource()))])};
      }
      if(stable(actual)!==stable(expected))fail('READBACK_FAILED','Created primitive geometry differs from requested geometry '+pid);
    }
    if(!plan.target_board&&after.routes.copper_pours.some(t=>t.source_kind==='Teardrop'&&!originalTeardrops.some(p=>p.id===t.id)))fail('TARGET_MISMATCH','New native copper requires a complete reviewed target_board');
    if(!plan.target_board&&!sameTraceCoverage(after.routes.traces,[...before.routes.traces.filter(t=>!plan.remove.traces.includes(t.id)),...plan.add.traces]))fail('READBACK_FAILED','Native trace copper differs from the complete requested trace set');
    if(plan.target_board&&boardGeometrySignature(after)!==boardGeometrySignature(plan.target_board)){
      const targetTraces=plan.target_board.routes?.traces??[];
      const equivalentTraces=sameTraceCoverage(targetTraces,after.routes.traces);
      const afterNormalized=equivalentTraces?{...copy(after),routes:{...copy(after.routes),traces:copy(targetTraces)}}:after;
      if(boardGeometrySignature(afterNormalized)!==boardGeometrySignature(plan.target_board)){
        const originalIds=new Set(originalTeardrops.map(t=>t.id)),createdIds=new Set(created.filter(p=>p.type==='Line'||p.type==='Via').map(p=>p.id));
        const addedTeardrops=afterNormalized.routes.copper_pours.filter(t=>t.source_kind==='Teardrop'&&!originalIds.has(t.id)),withoutAdded=copy(afterNormalized);
        withoutAdded.routes.copper_pours=withoutAdded.routes.copper_pours.filter(t=>!addedTeardrops.some(p=>p.id===t.id));
        if(addedTeardrops.length&&originalTeardrops.every(t=>afterNormalized.routes.copper_pours.some(p=>p.id===t.id&&eq(p.metadata?.native_source_record?.payload,t.metadata.native_source_record.payload)))&&
          addedTeardrops.every(t=>t.metadata.refs.some(ref=>createdIds.has(ref)))&&boardGeometrySignature(withoutAdded)===boardGeometrySignature(plan.target_board)){
          generatedNativeCopper={schema_version:1,source_only:true,source:copy(after.metadata.source),base_source:copy(plan.source),copper_pours:copy(addedTeardrops),
            geometry_signature_without_generated:boardGeometrySignature(withoutAdded),evidence:{source_only:true,baseline_teardrop_ids:[...originalIds],created_primitive_ids:[...createdIds]}};
        }
        fail('TARGET_MISMATCH','Native readback differs from target_board geometry');
      }
    }
    drc=await runNativeDrc(eda,uuid);event('native_drc',{passed:drc.passed,issues:drc.issues});
    if(!drc.passed)fail('NATIVE_DRC_FAILED','Native DRC returned issues');
    if(sourceFingerprint(await readSource(eda))!==after.metadata.source.fingerprint)fail('BOARD_CHANGED','Document changed during native DRC; refusing to save');
    journal.status='applied';journal.result_fingerprint=after.metadata.source.fingerprint;await store();
    let saved=false;
    if(opts.save===true){await activeDocument(eda,uuid);if(sourceFingerprint(await readSource(eda))!==after.metadata.source.fingerprint)fail('BOARD_CHANGED','Document changed before save');if(await eda.pcb_Document.save()!==true)fail('SAVE_FAILED','Native save failed or upload failed');saved=true;event('save');}
    return {success:true,applied:true,saved,plan_id:plan.plan_id,document_uuid:uuid,source:after.metadata.source,drc,events,backup_storage_key:key,live_board_validated:false};
  } catch(error) {
    const result={success:false,applied:false,saved:false,plan_id:plan.plan_id,error:{code:error.code??'API_ERROR',message:error.message,details:error.details??{}},drc,events,rollback:{attempted:false,restored:false},backup_storage_key:journal?key:null};
    if(generatedNativeCopper)result.generated_native_copper=generatedNativeCopper;
    if(edited&&original!==undefined) {
      result.rollback.attempted=true;
      try {
        await activeDocument(eda,uuid);
        if(await eda.sys_FileManager.setDocumentSource(original)!==true)fail('ROLLBACK_FAILED','setDocumentSource rejected backup');
        const proof=verifyRollbackSource(original,await readSource(eda));
        if(!proof.restored)fail('ROLLBACK_FAILED','Backup readback differs',{verification:proof});
        Object.assign(result.rollback,{restored:true,verification:proof.verification,source:{document_uuid:uuid,fingerprint:proof.source_fingerprint},requires_reexport:proof.requires_reexport});
        if(proof.requires_reexport)result.rollback.message='Record content restored with reordered records/tickets; re-export before preparing another plan';
        event('rollback',{restored:true,verification:proof.verification,source_fingerprint:proof.source_fingerprint,requires_reexport:proof.requires_reexport});
      }catch(rollbackError){result.rollback.error=rollbackError.message;event('rollback',{restored:false,error:rollbackError.message});}
    }
    if(journal){journal.status=result.rollback.attempted&&!result.rollback.restored?'recovery_required':'failed';journal.failure=result.error;try{await store();}catch(e){result.journal_error=e.message;}}
    return result;
  } finally {inflight.delete(uuid);}
}
