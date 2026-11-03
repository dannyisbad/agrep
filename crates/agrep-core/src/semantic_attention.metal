#include <metal_stdlib>
using namespace metal;

// One simdgroup owns a query row. K/V stay in their original strided layout.
// The four-key online-softmax tile is register-resident; no S x S buffer exists.
kernel void semantic_attention(
    device const half *q [[buffer(0)]], device const half *k [[buffer(1)]],
    device const half *v [[buffer(2)]], device const uint *lengths [[buffer(3)]],
    constant ulong4 &qs [[buffer(4)]], constant ulong4 &ks [[buffer(5)]],
    constant ulong4 &vs [[buffer(6)]], constant uint4 &shape [[buffer(7)]],
    constant float &scale [[buffer(8)]], constant int &radius [[buffer(9)]],
    device half *output [[buffer(10)]], uint tid [[thread_position_in_grid]]) {
    const uint row=tid/32, lane=tid%32;
    const uint batch=shape.x, heads=shape.y, seq=shape.z, width=shape.w;
    if(row>=batch*heads*seq) return;
    const uint pos=row%seq, h=(row/seq)%heads, b=row/(heads*seq);
    const uint n=lengths[b];
    const ulong out=(ulong(b)*seq*heads+ulong(pos)*heads+h)*width+lane;
    if(pos>=n){output[out]=half(0);if(width>32) output[out+32]=half(0);return;}
    const ulong qi=ulong(b)*qs.x+ulong(h)*qs.y+ulong(pos)*qs.z+lane;
    const float q0=float(q[qi]), q1=width>32?float(q[qi+32]):0;
    const uint low=radius<0?0:uint(max(0,int(pos)-radius));
    const uint high=radius<0?n:min(n,uint(int(pos)+radius+1));
    float maximum=-INFINITY, denominator=0, a0=0, a1=0;
    for(uint start=low;start<high;start+=4){
        float scores[4];
        float next=maximum;
        for(uint t=0;t<4;t++){
            const uint j=start+t;
            float score=-INFINITY;
            if(j<high){
                const ulong ki=ulong(b)*ks.x+ulong(h)*ks.y+ulong(j)*ks.z+lane;
                score=simd_sum(q0*float(k[ki])+(width>32?q1*float(k[ki+32]):0))*scale;
            }
            scores[t]=score;next=max(next,score);
        }
        const float correction=fast::exp(maximum-next);
        a0*=correction;a1*=correction;denominator*=correction;
        for(uint t=0;t<4;t++){
            const uint j=start+t;
            if(j<high){
                const float weight=fast::exp(scores[t]-next);
                const ulong vi=ulong(b)*vs.x+ulong(h)*vs.y+ulong(j)*vs.z+lane;
                a0=fma(weight,float(v[vi]),a0);if(width>32) a1=fma(weight,float(v[vi+32]),a1);
                denominator+=weight;
            }
        }
        maximum=next;
    }
    output[out]=half(a0/denominator);if(width>32) output[out+32]=half(a1/denominator);
}
