import assert from 'node:assert/strict';
import test from 'node:test';
import {atMean,catalogOrbit,trialOrbit,trialTrack} from '../coordinator/coordination/orbit-model.mjs';

const K=.01720209895, day=365.25;
const close=(v,w,tol=1e-8)=>v.forEach((x,i)=>assert.ok(Math.abs(x-w[i])<tol, [v,w]));
test('five-year markers begin at the epoch and follow the direction of motion',()=>{
  const a=100, period=2*Math.PI*a**1.5/(K*day);
  const model=trialOrbit([a,0,0],[0,0,K/Math.sqrt(a)]);
  assert.ok(Math.abs(model.period-period)<1e-9);
  const track=trialTrack(model,10);
  assert.equal(track.length,Math.ceil(period/5));
  close(track[0],[a,0,0]);
  const E=2*Math.PI*5/period;
  close(track[1],[a*Math.cos(E),0,a*Math.sin(E)]);
  close(atMean(model,model.m+2*Math.PI),track[0]);
});
test('inclined eccentric orbit reproduces both Cartesian position and velocity',()=>{
  const r=[367.2,-105.6,62.1],v=[-.0004,.00075,.00016],o=trialOrbit(r,v);
  close(atMean(o,o.m),r);
  const before=atMean(o,o.m-o.n*.1),after=atMean(o,o.m+o.n*.1);
  close(after.map((x,i)=>(x-before[i])/.2),v,1e-10);
});
test('open paths show a finite five-year arc through the reference point',()=>{
  const model=trialOrbit([100,0,0],[0,.003,.0001]);
  assert.ok(model.e>1);assert.equal(model.period,undefined);
  const track=trialTrack(model,1);assert.equal(track.length,201);
  close(track[100],[100,0,0]);assert.ok(track[99][1]<0 && track[101][1]>0);
});
test('catalog mean anomaly advances from its own epoch and node rotates the orbit',()=>{
  const b={a:40,e:0,i:90,om:90,w:0,ma:0,n:1,epoch:2460000};
  let o=catalogOrbit(b,b.epoch);close(atMean(o,o.m),[0,40,0]);
  o=catalogOrbit(b,b.epoch+90);close(atMean(o,o.m),[0,0,40]);
});
