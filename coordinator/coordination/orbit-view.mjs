import {atMean, catalogOrbit, ellipsePoint, trialOrbit, trialTrack} from './orbit-model.mjs?v=3';

const TAU = 2*Math.PI;
const palette = {trial:'#ffbd77', planets:'#a8c7f1', largest:'#7bd7c4', detached:'#b8a0ec', sun:'#fff0b0'};
const clamp = (v,low,high) => Math.max(low,Math.min(high,v));
let catalogRequest;
function loadCatalog() {
  catalogRequest ??= fetch(new URL('orbit-catalog.json?v=1',import.meta.url)).then(r => {
    if (!r.ok) throw new Error('Catalog unavailable');
    return r.json();
  }).catch(error => { catalogRequest=null; throw error; });
  return catalogRequest;
}

function mount(root,catalog) {
  const data=JSON.parse(root.querySelector('.orbit-data').textContent);
  const canvas=root.querySelector('canvas'), ctx=canvas.getContext('2d');
  const output=root.querySelector('output');
  const resetButton=root.querySelector('[data-reset]');
  const inputs=[...root.querySelectorAll('[data-layer]')];
  const trial=trialOrbit(data.position,data.velocity);
  const relative=p => p.map((value,i) => value-data.sun[i]);
  const current=relative(data.position);
  const fullSpan=trial?.period ? 2.4*trial.a : Math.max(200,2.5*Math.hypot(...current));
  const orbitCenter=trial?.period ? relative(trial.p.map(x => -trial.a*trial.e*x)) : [0,0,0];
  const planets=catalog.planets.map(body => marker(body,true));
  const kbos=catalog.kbos.map(body => marker(body,false));
  const detached=kbos.filter(body => body.detached);
  const sun={name:'Sun',point:[0,0,0],color:palette.sun};
  const target={name:'candidate location · '+data.epoch+' UTC',point:current,color:palette.trial};
  const pointers=new Map();
  let yaw,tilt,center,span,panX,panY,W,H,scale,scheduled=false,hit=[],selected=null,gesture=null;
  let track=[],lastScale=0,lastTap=null;
  function marker(body,planet) {
    const o=catalogOrbit(body,data.jd);
    const center=o.p.map(value => -o.a*o.e*value);
    const outline=[center, ellipsePoint(o,0), ellipsePoint(o,Math.PI/2)];
    return {...body,orbit:o,outline,point:atMean(o,o.m),planet,color:planet?palette.planets:body.rank?palette.largest:palette.detached};
  }
  function reset() {
    yaw=-.32; tilt=.32; panX=panY=0; center=orbitCenter;
    span=fullSpan;
    selected=null; output.textContent=''; request();
  }
  function request() {
    if (!scheduled) { scheduled=true; requestAnimationFrame(draw); }
  }
  function draw() {
    scheduled=false;
    resetButton.hidden=Math.abs(span/fullSpan-1)<1e-9;
    W=canvas.clientWidth; H=canvas.clientHeight;
    if (!W || !H) return;
    const dpr=Math.min(2,window.devicePixelRatio||1);
    if (canvas.width!==Math.round(W*dpr) || canvas.height!==Math.round(H*dpr)) {
      canvas.width=Math.round(W*dpr); canvas.height=Math.round(H*dpr);
    }
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,W,H); scale=Math.min(W,H)/span; hit=[];
    const cy=Math.cos(yaw),sy=Math.sin(yaw),ct=Math.cos(tilt),st=Math.sin(tilt);
    function project(p) {
      const x=p[0]-center[0], y=p[1]-center[1], z=p[2]-center[2];
      return [W/2+panX+scale*(cy*x-sy*y),H/2+panY-scale*((sy*x+cy*y)*ct+z*st)];
    }
    function line(points,color,width,closed=false,offset=false) {
      ctx.beginPath();
      points.forEach((v,i) => {
        const p=project(offset?relative(v):v);
        if (i===0) ctx.moveTo(...p); else ctx.lineTo(...p);
      });
      if (closed) ctx.closePath();
      ctx.strokeStyle=color; ctx.lineWidth=width; ctx.stroke();
    }
    function outlines(bodies,color,width) {
      // The sky-plane projection of an orbit is an affine image of a unit circle.
      // Restore the canvas transform before stroking so every orbit gets the same hairline.
      ctx.beginPath();
      for (const body of bodies) {
        const [center,major,minor]=body.outline.map(project);
        ctx.save();
        ctx.transform(major[0]-center[0],major[1]-center[1],
                      minor[0]-center[0],minor[1]-center[1],center[0],center[1]);
        ctx.moveTo(1,0);
        ctx.arc(0,0,1,0,TAU);
        ctx.closePath();
        ctx.restore();
      }
      ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();
    }
    function mark(body,r,label=false) {
      const [x,y]=project(body.point);
      if (x<0 || y<0 || x>W || y>H) return;
      ctx.fillStyle=body.color;
      if (body.orbit && !body.planet) {
        ctx.fillRect(x-r,y-r,2*r,2*r);
      } else {
        ctx.beginPath();ctx.arc(x,y,r,0,TAU);ctx.fill();
      }
      if (label) {ctx.font='11px system-ui';ctx.fillStyle=body.color;ctx.fillText(body===target?'candidate location':body.name,x+7,y-6);}
      hit.push({x,y,body});
    }
    const layers=Object.fromEntries(inputs.map(el=>[el.dataset.layer,el.checked]));
    const step=niceLength(span/3);
    for (const radius of [step,step*2,step*3]) {
      line(Array.from({length:97},(_,i) => [radius*Math.cos(i*TAU/96),radius*Math.sin(i*TAU/96),0]),'#233947',.65);
    }
    const extent=Math.max(span*3,60);
    line([[-extent,0,0],[extent,0,0]],'#243a48',.65);
    line([[0,-extent,0],[0,extent,0]],'#243a48',.65);
    if (layers.detached) outlines(detached,'#b8a0ec60',.4);
    if (layers.planets) outlines(planets,'#6387ae88',.55);
    if (selected?.orbit && !selected.planet) outlines([selected],selected.color,.65);
    if (trial) {
      if (lastScale!==scale) {track=trialTrack(trial,scale);lastScale=scale;}
      line(track,palette.trial,.45,Boolean(trial.period),true);
      ctx.fillStyle=palette.trial;ctx.beginPath();
      let lastX=Infinity,lastY=Infinity;
      for (const v of track) {
        const [x,y]=project(relative(v));
        if (x<0 || y<0 || x>W || y>H || (x-lastX)**2+(y-lastY)**2<1.6) continue;
        ctx.moveTo(x+.8,y);ctx.arc(x,y,.8,0,TAU);lastX=x;lastY=y;
      }
      ctx.fill();
    }
    for (const body of kbos) {
      if ((layers.largest && body.rank) || (layers.detached && body.detached)) {
        body.color=layers.largest && body.rank?palette.largest:palette.detached;
        mark(body,body.rank && layers.largest?1:.75);
      }
    }
    if (layers.planets) {
      const labels=[];
      for (const body of [...planets].reverse()) {
        const [x,y]=project(body.point), show=span<170 && labels.every(p=>Math.hypot(x-p[0],y-p[1])>34);
        mark(body,2.15,show);
        if(show) labels.push([x,y]);
      }
    }
    mark(sun,3);mark(target,3.2,true);
    if(selected && (!selected.orbit || selected.planet)) {
      const [x,y]=project(selected.point);ctx.strokeStyle='#fff';ctx.lineWidth=1;
      ctx.beginPath();ctx.arc(x,y,6,0,TAU);ctx.stroke();
    }
    const length=niceLength(68/scale);
    ctx.fillStyle='#bfccd4';ctx.strokeStyle='#bfccd4';ctx.lineWidth=1;
    ctx.font='10px system-ui';ctx.fillText(length+' au',14,H-19);
    ctx.beginPath();ctx.moveTo(14,H-15);ctx.lineTo(14,H-11);ctx.lineTo(14+length*scale,H-11);ctx.lineTo(14+length*scale,H-15);ctx.stroke();
    ctx.textAlign='right';ctx.fillText('J2000 ecliptic',W-10,H-12);ctx.textAlign='left';
  }
  function zoom(factor,x=W/2,y=H/2) {
    const next=clamp(span/factor,.2,Math.max(5e4,4*(trial?.a||0)));
    const ratio=span/next;
    panX=(panX+W/2-x)*ratio+x-W/2;panY=(panY+H/2-y)*ratio+y-H/2;
    span=next;request();
  }
  function pick(x,y) {
    const candidates=hit.map(h=>({...h,d:Math.hypot(x-h.x,y-h.y)})).filter(h=>h.d<10).sort((a,b)=>a.d-b.d);
    selected=candidates[0]?.body??null;
    output.textContent=selected ? [selected.name,selected.rank?'size rank '+selected.rank:null,
      selected.diameter_km?selected.diameter_km+' km':null,
      selected.detached?(selected.detached==='S'?'detached':'detached (uncertain)'):null].filter(Boolean).join(' · ') : '';
    request();
  }
  const location=e=>{const b=canvas.getBoundingClientRect();return [e.clientX-b.left,e.clientY-b.top];};
  canvas.addEventListener('pointerdown',e=>{
    e.preventDefault();canvas.focus({preventScroll:true});canvas.setPointerCapture(e.pointerId);
    const p=location(e);pointers.set(e.pointerId,p);gesture={first:p,moved:pointers.size>1,pan:e.shiftKey || e.button===2};
    canvas.classList.toggle('panning',gesture.pan);
  });
  canvas.addEventListener('pointermove',e=>{
    if (!pointers.has(e.pointerId)) return;
    const old=pointers.get(e.pointerId), p=location(e), dx=p[0]-old[0],dy=p[1]-old[1];
    if (pointers.size===2) {
      const other=[...pointers].find(([id])=>id!==e.pointerId)[1];
      const before=Math.hypot(old[0]-other[0],old[1]-other[1]),after=Math.hypot(p[0]-other[0],p[1]-other[1]);
      if(before>1 && after>1) zoom(after/before,(old[0]+other[0])/2,(old[1]+other[1])/2);
      panX+=dx/2;panY+=dy/2;
    } else if(gesture.pan || e.shiftKey || e.buttons===2) {
      gesture.pan=true;canvas.classList.add('panning');panX+=dx;panY+=dy;
    }
    else {yaw+=dx*.009;tilt=clamp(tilt+dy*.009,-Math.PI/2,Math.PI/2);}
    gesture.moved ||= Math.hypot(p[0]-gesture.first[0],p[1]-gesture.first[1])>3;
    pointers.set(e.pointerId,p);request();
  });
  function release(e) {
    pointers.delete(e.pointerId);
    if (!pointers.size) canvas.classList.remove('panning');
  }
  canvas.addEventListener('pointerup',e=>{
    const p=location(e);
    if (gesture && !gesture.moved && !gesture.pan) {
      const twice=lastTap && lastTap.type===e.pointerType && e.timeStamp-lastTap.time<450 &&
        Math.hypot(p[0]-lastTap.p[0],p[1]-lastTap.p[1])<16;
      if (twice) {
        panX+=W/2-p[0];panY+=H/2-p[1];lastTap=null;request();
      } else {
        pick(...p);lastTap={p,time:e.timeStamp,type:e.pointerType};
      }
    } else lastTap=null;
    release(e);
  });
  canvas.addEventListener('pointercancel',e=>{lastTap=null;release(e);});
  canvas.addEventListener('lostpointercapture',release);
  canvas.addEventListener('contextmenu',e=>e.preventDefault());
  canvas.addEventListener('wheel',e=>{
    e.preventDefault();
    const unit=e.deltaMode===1?16:e.deltaMode===2?H:1;
    if(e.shiftKey) {panX-=e.deltaX*unit;panY-=e.deltaY*unit;request();}
    else zoom(Math.exp(clamp(-e.deltaY*unit*.002,-.9,.9)),...location(e));
  },{passive:false});
  canvas.addEventListener('keydown',e=>{
    if(e.key==='+'||e.key==='=')zoom(1.4);else if(e.key==='-')zoom(1/1.4);
    else if(e.key==='ArrowLeft')yaw-=.12;else if(e.key==='ArrowRight')yaw+=.12;
    else if(e.key==='ArrowUp')tilt=clamp(tilt+.12,-Math.PI/2,Math.PI/2);
    else if(e.key==='ArrowDown')tilt=clamp(tilt-.12,-Math.PI/2,Math.PI/2);
    else if(e.key==='Home')reset();else return;e.preventDefault();request();
  });
  root.querySelector('[data-center]').onclick=()=>{
    center=selected?.point ?? orbitCenter;panX=panY=0;request();
  };
  resetButton.onclick=reset;
  root.querySelector('[data-zoom="in"]').onclick=()=>zoom(1.6);
  root.querySelector('[data-zoom="out"]').onclick=()=>zoom(1/1.6);
  inputs.forEach(el=>el.onchange=()=>{selected=null;output.textContent='';request();});
  new ResizeObserver(request).observe(canvas);
  reset();
}
function niceLength(value) {
  const power=10**Math.floor(Math.log10(value)), m=value/power;
  return Number(((m>=5?5:m>=2?2:1)*power).toPrecision(2));
}
const observer=new IntersectionObserver(entries=>{
  for(const e of entries) if(e.isIntersecting) {
    observer.unobserve(e.target);
    loadCatalog().then(c=>mount(e.target,c)).catch(()=>{e.target.querySelector('output').textContent='Orbit diagram unavailable.';});
  }
},{rootMargin:'180px'});
document.querySelectorAll('.orbit-view').forEach(root=>observer.observe(root));
