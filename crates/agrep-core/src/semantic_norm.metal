#include <metal_stdlib>
using namespace metal;
kernel void residual_norm(device const half *x [[buffer(0)]],device const half *r [[buffer(1)]],
 device const half *w [[buffer(2)]],device half *out [[buffer(3)]],constant ulong &width [[buffer(4)]],
 constant ulong &count [[buffer(5)]],constant float &eps [[buffer(6)]],uint row [[threadgroup_position_in_grid]],
 uint lane [[thread_index_in_threadgroup]]) {
 threadgroup float totals[128],squares[128];
 float sum=0,square=0;
 for(ulong i=lane;i<width;i+=128){
   const ulong at=ulong(row)*width+i;
   const half v=half(float(x[at])+float(r[at]));
   out[at]=v;sum+=float(v);square+=float(v)*float(v);
 }
 totals[lane]=sum;squares[lane]=square;threadgroup_barrier(mem_flags::mem_threadgroup);
 for(uint stride=64;stride>0;stride/=2){
   if(lane<stride){totals[lane]+=totals[lane+stride];squares[lane]+=squares[lane+stride];}
   threadgroup_barrier(mem_flags::mem_threadgroup);
 }
 const float mean=totals[0]/float(width),inv=rsqrt(squares[0]/float(width)-mean*mean+eps);
 for(ulong i=lane;i<width;i+=128){const ulong at=ulong(row)*width+i;out[count+at]=half((float(out[at])-mean)*inv*float(w[i]));}
}
