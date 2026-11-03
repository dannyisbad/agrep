// Candle 0.9.2's erf polynomial (MIT/Apache-2.0), fused with the gate.
#include <metal_stdlib>
using namespace metal;
float semantic_erf(float x) {
    float sign=x<0 ? -1.0f : 1.0f;
    x=abs(x);
    float t=1.0f/(1.0f+0.3275911f*x);
    float y=1.0f-(((((1.061405429f*t-1.453152027f)*t)+1.421413741f)*t-0.284496736f)*t+0.254829592f)*t*exp(-x*x);
    return sign*y;
}
kernel void semantic_geglu(constant size_t &count,constant size_t &width,
                           device const half *input,device half *output,
                           uint i [[thread_position_in_grid]]) {
    if(i>=count) return;
    size_t offset=(i/width)*(width*2)+i%width;
    float x=float(input[offset]);
    // Preserve the F16 rounding between Candle's unary and binary kernels.
    half activated=half(x*(1.0f+semantic_erf(x*M_SQRT1_2_F))/2.0f);
    output[i]=activated*input[offset+width];
}
