#include <metal_stdlib>
using namespace metal;
kernel void attention_masks(device const uint *lengths [[buffer(0)]],device half *global_mask [[buffer(1)]],
 device half *local_mask [[buffer(2)]],constant uint &seq [[buffer(3)]],constant uint &batch [[buffer(4)]],
 constant uint &radius [[buffer(5)]],uint i [[thread_position_in_grid]]) {
 if(i>=batch*seq*seq)return;
 const uint key=i%seq,query=(i/seq)%seq,b=i/(seq*seq);
 const bool padding=key>=lengths[b];
 if(query==0)global_mask[b*seq+key]=padding?half(-65504):half(0);
 local_mask[i]=(padding||abs(int(query)-int(key))>int(radius))?half(-65504):half(0);
}
kernel void embedding_norm(device const uint *ids [[buffer(0)]],device const half *embedding [[buffer(1)]],
 device const half *weight [[buffer(2)]],device half *out [[buffer(3)]],constant ulong &width [[buffer(4)]],
 constant ulong &count [[buffer(5)]],constant float &eps [[buffer(6)]],uint row [[threadgroup_position_in_grid]],
 uint lane [[thread_index_in_threadgroup]]) {
 threadgroup float totals[128],squares[128];
 float sum=0,square=0;
 for(ulong i=lane;i<width;i+=128){float v=float(embedding[ulong(ids[row])*width+i]);sum+=v;square+=v*v;}
 totals[lane]=sum;squares[lane]=square;threadgroup_barrier(mem_flags::mem_threadgroup);
 for(uint stride=64;stride>0;stride/=2){if(lane<stride){totals[lane]+=totals[lane+stride];squares[lane]+=squares[lane+stride];}threadgroup_barrier(mem_flags::mem_threadgroup);}
 const float mean=totals[0]/float(width),inv=rsqrt(squares[0]/float(width)-mean*mean+eps);
 for(ulong i=lane;i<width;i+=128){const ulong at=ulong(row)*width+i;half v=half((float(embedding[ulong(ids[row])*width+i])-mean)*inv*float(weight[i]));out[at]=v;out[count+at]=v;}
}
kernel void packed_rope(device const half *qkv [[buffer(0)]],device const half *cosine [[buffer(1)]],
 device const half *sine [[buffer(2)]],device half *q [[buffer(3)]],device half *k [[buffer(4)]],
 constant uint4 &shape [[buffer(5)]],uint i [[thread_position_in_grid]]) {
 const uint b=shape.x,h=shape.y,s=shape.z,d=shape.w;
 if(i>=b*h*s*d/2) return;
 const uint halfd=d/2, part=i%halfd, pos=(i/halfd)%s, head=(i/(halfd*s))%h, batch=i/(halfd*s*h);
 const ulong source=((ulong(batch)*s+pos)*3*h+head)*d+part;
 const ulong dest=((ulong(batch)*h+head)*s+pos)*d+part;
 const half c=cosine[pos*halfd+part], sn=sine[pos*halfd+part];
 const half q0=qkv[source],q1=qkv[source+halfd],k0=qkv[source+h*d],k1=qkv[source+h*d+halfd];
 q[dest]=q0*c-q1*sn;q[dest+halfd]=q0*sn+q1*c;
 k[dest]=k0*c-k1*sn;k[dest+halfd]=k0*sn+k1*c;
}
