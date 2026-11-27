// J2000 ecliptic axes, au and Julian days. The trial uses the search's solar point force.
const TAU = 2 * Math.PI;
const RAD = Math.PI / 180;
const MU = 0.01720209895 ** 2;
const dot = (a, b) => a.reduce((s, x, i) => s + x*b[i], 0);
const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
const norm = v => Math.hypot(...v);
const unit = v => v.map(x => x/norm(v));

function eccentricAnomaly(mean, e) {
  const m = mean - TAU * Math.round(mean / TAU);
  let lo = -Math.PI, hi = Math.PI, x = e < .8 ? m : m + .85 * e * Math.sign(m);
  for (let j = 0; j < 45; j++) {
    const error = x - e*Math.sin(x) - m;
    if (Math.abs(error) < 2e-14) break;
    if (error < 0) lo = x; else hi = x;
    const next = x - error / (1-e*Math.cos(x));
    x = next > lo && next < hi ? next : (lo+hi)/2;
  }
  return x;
}

function planePoint(o, x, y) {
  return o.p.map((value,i) => o.a*(value*x + o.q[i]*y));
}
export function ellipsePoint(o, E) {
  return planePoint(o, Math.cos(E)-o.e, Math.sqrt((1-o.e)*(1+o.e))*Math.sin(E));
}
export function atMean(o, m) {
  return ellipsePoint(o, eccentricAnomaly(m,o.e));
}

export function catalogOrbit(body, jd) {
  let a,e,i,node,arg,mean;
  if (body.base) {
    const t = (jd-2451545)/36525;
    const [axis,ecc,inc,L,peri,asc] = body.base.map((x,j) => x+t*body.rates[j]);
    [a,e,i,node,arg,mean] = [axis,ecc,inc,asc,peri-asc,L-peri];
  } else {
    ({a,e,i,om:node,w:arg} = body);
    mean = body.ma + (jd-body.epoch)*body.n;
  }
  const w=arg*RAD, O=node*RAD, I=i*RAD;
  const cw=Math.cos(w), sw=Math.sin(w), cO=Math.cos(O), sO=Math.sin(O), cI=Math.cos(I), sI=Math.sin(I);
  return {a,e,m:mean*RAD,p:[cO*cw-sO*sw*cI,sO*cw+cO*sw*cI,sw*sI],
    q:[-cO*sw-sO*cw*cI,-sO*sw+cO*cw*cI,cw*sI]};
}

export function trialOrbit(r, v) {
  const R=norm(r), h=cross(r,v), a=1/(2/R-dot(v,v)/MU);
  const eccentricity=cross(v,h).map((x,i) => x/MU-r[i]/R);
  const e=norm(eccentricity);
  if (!(norm(h)>0) || !Number.isFinite(a) || Math.abs(e-1)<1e-12) return null;
  const p=e>1e-12 ? unit(eccentricity) : unit(r), q=cross(unit(h),p);
  const n=Math.sqrt(MU/Math.abs(a)**3);
  if (e>1) {
    const H=Math.asinh(dot(r,q)/(-a*Math.sqrt(e*e-1)));
    return {a,e,p,q,n,m:e*Math.sinh(H)-H};
  }
  const E=Math.atan2(dot(r,q)/(a*Math.sqrt(1-e*e)),dot(r,p)/a+e);
  return {a,e,p,q,n,m:E-e*Math.sin(E),period:TAU/(n*365.25)};
}

function hyperbolicPoint(o, years) {
  const m=o.m+o.n*365.25*years;
  let H=Math.asinh(m/o.e);
  for (let j=0; j<30; j++) {
    const step=(o.e*Math.sinh(H)-H-m)/(o.e*Math.cosh(H)-1);
    H-=step;
    if (Math.abs(step)<1e-13) break;
  }
  return planePoint(o,Math.cosh(H)-o.e,-Math.sqrt(o.e*o.e-1)*Math.sinh(H));
}

export function trialPositionAt(orbit, days) {
  if (!orbit) throw new Error('This path has no usable trial orbit.');
  if (orbit.e > 1) return hyperbolicPoint(orbit, days / 365.25);
  return atMean(orbit, orbit.m + orbit.n * days);
}

// Each marker time is an integer multiple of five years from the displayed epoch.
// At extreme periods, suppress coincident subpixel markers using the maximum orbital speed.
export function trialTrack(o, pixelsPerAU) {
  if (!o) return [];
  if (o.e>1) {
    return Array.from({length: 201}, (_,j) => hyperbolicPoint(o,(j-100)*5));
  }
  const count=Math.ceil(o.period/5);
  const fastestStep=5*365.25*Math.sqrt(MU/o.a*(1+o.e)/(1-o.e));
  const stride=Math.max(1,Math.floor(.65/(fastestStep*pixelsPerAU)));
  const points=[];
  for (let j=0; j<count; j+=stride) points.push(atMean(o,o.m+j*5*365.25*o.n));
  return points;
}
