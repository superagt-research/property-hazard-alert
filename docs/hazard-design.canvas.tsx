import { useHostTheme, useState } from "cursor/canvas";

const sources = [
  {id:"nws",name:"NWS CAP",hazard:"风、龙卷风、强对流、洪水、冬季、冻损、火险天气",poll:"60 秒",result:"35 条 / 完整",geometry:"7 个原生 polygon；28 个 zone fallback",scope:"全国 NWS 产品；按美国投保地点后置匹配",detail:"Watch、Warning、雷达指示与观测分开。Red Flag 属于 fire weather，不能当作已起火。批量 zones 返回空几何，单 zone 备用接口补齐。",link:"https://www.weather.gov/documentation/services-web-alerts",time:"2026-09-14 12:56:17–12:56:39 UTC"},
  {id:"usgs",name:"USGS + ShakeMap",hazard:"地震与模型估计的地面震动",poll:"60 秒",result:"38 条 / 完整",geometry:"38 个震中点；4 份 ShakeMap grid",scope:"全球 M2.5+ past-day；不是美国地震数量",detail:"几何采用二维坐标，深度另存 km。图外采样返回 unknown。MMI/PGA 是震动估计，不能直接转换为建筑损失或费率。",link:"https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",time:"2026-09-14 13:05:36–13:05:39 UTC"},
  {id:"nifc",name:"NIFC / WFIGS",hazard:"野火事件与已知边界",poll:"5 分钟查变更",result:"446 条 / 完整",geometry:"180 个 perimeter；266 个事件点",scope:"Current WF；仅点记录保留范围未知",detail:"156 个 perimeter 已超过24小时，18 个缺观测时间。100% containment 不等于 fire out。生产应按 ID/修改版本增量下载，不重复全量拉大边界。",link:"https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0",time:"2026-09-14 12:58:34–12:58:48 UTC"},
  {id:"nhc",name:"NHC",hazard:"热带气旋、轨迹与风圈背景",poll:"活跃时 5 分钟",result:"14 条 / 部分完整",geometry:"2 个中心；12 条有效空间记录",scope:"2 个海盆气旋；不是美国受灾区计数",detail:"一个零面积初始风圈被隔离。Cone 不是 wind footprint。5 个 forecast wind geometry 缺逐面有效时间，不能直接驱动自动停保。",link:"https://www.nhc.noaa.gov/aboutnhcgraphics.shtml",time:"2026-09-14 12:58:08–12:58:09 UTC"},
  {id:"nwps",name:"NOAA NWPS",hazard:"河道观测与洪水预报补充",poll:"15 分钟",result:"22 条 / 样本完整",geometry:"2,710 个 gauge 中的22条状态点",scope:"东南部 bbox 样本；全国请求曾超时",detail:"18 条为 action stage，只标 potential。水位 sentinel 转 null，有效负水位保留；gauge 点不是淹水 polygon。另一次同区域请求也曾超时，生产应切片。",link:"https://api.water.noaa.gov/about/api",time:"2026-09-14 13:05:39–13:06:00 UTC"},
];
const modes = [
  {id:"alert",name:"Alert",flow:["官方免费数据", "统一 ETL + 原始留存", "PostGIS 地点匹配 + 时效", "平台 Alert API", "Underwriter 查看"],outcome:"提示与人工核保；现有报价/出单逻辑可以暂不改变。",gate:"统一后端必须存在：数据一致性、几何匹配、缓存和证据历史。"},
  {id:"shadow",name:"Shadow",flow:["同一事件与地点服务", "已审批的规则版本", "计算拟议业务决定", "记录与实际结果对照", "Underwriter 校准规则"],outcome:"记录拟议的 ALLOW / REFER / BLOCK / UNKNOWN；不执行新停保决定。",gate:"评价误报、漏报、数据未知和受影响交易，再决定是否启用 enforce。"},
  {id:"enforce",name:"Moratorium",flow:["同一事件与地点服务", "规则 + Feature flag 模式", "Bind / Issue 最终重评", "版本一致性与事务保护", "决定/证据/Override 审计"],outcome:"服务端按批准政策约束指定交易。产品、保障、地点、动作和时间都属于规则范围。",gate:"Feature flag 管 off/shadow/enforce；独立政策管何地、何时、何产品、何种交易受限。"},
];

export default function HazardDesign() {
  const t = useHostTheme();
  const [selected, setSelected] = useState("nws");
  const [mode, setMode] = useState("alert");
  const source = sources.find(s=>s.id===selected)!;
  const phase = modes.find(s=>s.id===mode)!;
  const border = `1px solid ${t.stroke.primary}`;
  const button = (active:boolean) => ({border,background:active?t.fill.primary:t.bg.editor,color:active?t.accent.primary:t.text.secondary,padding:"7px 12px",cursor:"pointer",fontSize:13});
  return <main style={{fontFamily:"system-ui,sans-serif",color:t.text.primary,background:t.bg.editor,padding:24,maxWidth:1180,margin:"0 auto",fontSize:14,lineHeight:1.65}}>
    <div style={{fontSize:12,color:t.text.secondary}}>美国商业财产险 · 设计与真实数据验证 · 2026-09-14</div>
    <h1 style={{fontSize:24,margin:"4px 0 12px"}}>先建立可信告警，再加入承保政策</h1>
    <p style={{maxWidth:900,margin:"0 0 24px"}}>Alert 阶段也采用统一后端。前端呈现证据；moratorium 阶段由 bind / issue 服务端执行版本化政策。当前成果为 ETL 与架构设计，没有部署。</p>
    <nav style={{display:"flex",gap:8,marginBottom:14}} aria-label="实施阶段">{modes.map(m=><button key={m.id} style={button(m.id===mode)} onClick={()=>setMode(m.id)} aria-pressed={m.id===mode}>{m.name}</button>)}</nav>
    <section style={{padding:"18px 0",borderTop:border,borderBottom:border}}>
      <div style={{display:"flex",flexWrap:"wrap",gap:8,alignItems:"center"}}>{phase.flow.map((s,i)=><div key={s} style={{display:"flex",alignItems:"center",gap:8}}><span style={{padding:"9px 12px",background:t.fill.tertiary}}>{s}</span>{i<phase.flow.length-1&&<span style={{color:t.text.tertiary}}>→</span>}</div>)}</div>
      <p style={{margin:"14px 0 3px",color:t.accent.primary}}>{phase.outcome}</p>
      <div style={{color:t.text.secondary}}>{phase.gate}</div>
    </section>
    <h2 style={{fontSize:18,margin:"28px 0 10px"}}>数据源选择与采集周期</h2>
    <div style={{overflowX:"auto"}}><table style={{width:"100%",borderCollapse:"collapse",textAlign:"left",fontSize:13}}>
      <caption style={{textAlign:"left",captionSide:"bottom",fontSize:12,color:t.text.secondary,paddingTop:8}}>来源：各官方接口的本地 run manifest。各行采集时点与scope不同；记录数不能相加为美国灾害总数。周期为建议，非源 SLA。</caption>
      <thead><tr>{["官方来源","Property 相关 hazard","建议轮询","实际归一化结果"].map(h=><th key={h} style={{borderBottom:border,padding:"8px 10px",fontWeight:600}}>{h}</th>)}</tr></thead>
      <tbody>{sources.map(s=><tr key={s.id} style={{background:s.id===selected?t.fill.tertiary:undefined}}><td style={{borderBottom:border,padding:"10px"}}><button onClick={()=>setSelected(s.id)} aria-pressed={s.id===selected} style={{border:0,background:"transparent",color:t.text.link,cursor:"pointer",fontSize:13,textAlign:"left",padding:0}}>{s.name}</button></td><td style={{borderBottom:border,padding:10}}>{s.hazard}</td><td style={{borderBottom:border,padding:10,whiteSpace:"nowrap"}}>{s.poll}</td><td style={{borderBottom:border,padding:10,whiteSpace:"nowrap"}}>{s.result}</td></tr>)}</tbody>
    </table></div>
    <section style={{padding:"18px 0",display:"grid",gridTemplateColumns:"minmax(190px,1fr) minmax(260px,2fr)",gap:24}}>
      <div><h3 style={{fontSize:16,margin:"0 0 5px"}}>{source.name}</h3><div>{source.geometry}</div><div style={{fontSize:12,color:t.text.secondary,marginTop:8}}>{source.time}</div><a href={source.link} style={{color:t.text.link,fontSize:12}}>官方来源说明</a></div>
      <div><strong style={{fontWeight:600}}>{source.scope}</strong><p style={{margin:"5px 0",color:t.text.secondary}}>{source.detail}</p></div>
    </section>
    <section style={{borderTop:border,paddingTop:18,display:"grid",gridTemplateColumns:"1fr 1fr",gap:28}}>
      <div><h2 style={{fontSize:18,margin:"0 0 8px"}}>保存三类事实</h2><ol style={{paddingLeft:20,margin:0}}><li>S3：原始响应、版本、时间与哈希。</li><li>PostGIS：事件、几何角色、区域与位置版本。</li><li>审计：来源快照、政策版本、动作、决定与审批。</li></ol></div>
      <div><h2 style={{fontSize:18,margin:"0 0 8px"}}>保留未知状态</h2><ul style={{paddingLeft:20,margin:0}}><li>数据失联或几何缺失，不能转成 clear。</li><li>事件过期/消失，不自动解除公司 moratorium。</li><li>48项测试通过；重复载入新增修订为0。</li></ul></div>
    </section>
    <footer style={{borderTop:border,marginTop:24,paddingTop:12,fontSize:12,color:t.text.secondary}}>已实现：五类ETL、本地raw/JSONL/SQLite、语义测试与哈希核对。待实施：AWS、真实API、全国portfolio空间匹配、长期回补与承保规则。FEMA声明可每小时补充行政上下文，本轮未实现adapter。Canvas以固定验证快照展示，不联网刷新。</footer>
  </main>;
}
